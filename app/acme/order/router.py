import asyncio
import secrets
from typing import Annotated, Literal, Optional
from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, conlist, constr

from ..certificate.service import check_csr, SerialNumberConverter
from ..exceptions import ACMEException
from ..middleware import RequestData, SignedRequest
import db
from config import settings
from jwcrypto.common import base64url_decode
from datetime import datetime
from ca import service as ca_service
from logger import logger

class NewOrderDomain(BaseModel):
    type: Literal['dns']
    value: constr(regex=f'^{settings.acme.target_domain_regex.pattern}$')

class NewOrderPayload(BaseModel):
    identifiers: conlist(NewOrderDomain, min_items=1)
    notBefore: Optional[datetime] = None  # not evaluated
    notAfter: Optional[datetime] = None  # not evaluated

class FinalizeOrderPayload(BaseModel):
    csr: constr(min_length=1, max_length=1*1024**2)

def order_response(*, 
    status: str, expires_at: datetime, domains: list[str], authz_ids: list[str], order_id: str, error: Optional[ACMEException] = None,
    not_valid_before: Optional[datetime] = None, not_valid_after: Optional[datetime] = None, cert_serial_number: Optional[str] = None):
    return {
            "status": status,
            "expires": expires_at,
            "identifiers": [{ "type": "dns", "value": domain} for domain in domains],
            "authorizations": [f'{settings.external_uri}/acme/authorizations/{authz_id}' for authz_id in authz_ids],
            "finalize": f'{settings.external_uri}/acme/orders/{order_id}/finalize',
            "error": error.value if error else None,
            "notBefore": not_valid_before,
            "notAfter": not_valid_after,
            "certificate": f"{settings.external_uri}/acme/certificates/{cert_serial_number}" if cert_serial_number else None
        }


api = APIRouter(tags=['acme:order'])

@api.post('/new-order', status_code=201)
async def submit_order(response: Response, data: Annotated[RequestData[NewOrderPayload], Depends(SignedRequest(NewOrderPayload))]):
    domains: list[str] = [identifier.value for identifier in data.payload.identifiers]

    def generate_tokens_sync(domains):
        order_id = secrets.token_urlsafe(16)
        authz_ids = {domain: secrets.token_urlsafe(16) for domain in domains}
        chal_ids = {domain: secrets.token_urlsafe(16) for domain in domains}
        chal_tkns = {domain: secrets.token_urlsafe(32) for domain in domains}
        return order_id, authz_ids, chal_ids, chal_tkns

    order_id, authz_ids, chal_ids, chal_tkns = await asyncio.to_thread(generate_tokens_sync, domains)

    async with db.transaction() as sql:
        order_status, expires_at = await sql.record('''
            insert into orders (id, account_id) values ($1, $2)
            returning status, expires_at
        ''', order_id, data.account_id)
        await sql.execmany('''insert into authorizations (id, order_id, domain) values ($1, $2, $3)''', 
            *[( authz_ids[domain], order_id, domain) for domain in domains])
        await sql.execmany('''insert into challenges (id, authz_id, token) values ($1, $2, $3)''', 
            *[( chal_ids[domain], authz_ids[domain], chal_tkns[domain] ) for domain in domains])

    response.headers["Location"] = f'{settings.external_uri}/acme/orders/{order_id}'
    return order_response(status=order_status, expires_at=expires_at, domains=domains, authz_ids=authz_ids.values(), order_id=order_id)


@api.post('/orders/{order_id}')
async def view_order(response: Response, order_id: str, data: Annotated[RequestData, Depends(SignedRequest())]):
    async with db.transaction(readonly=True) as sql:
        record = await sql.record('''
            select status, expires_at, error from orders where id = $1 and account_id = $2
        ''', order_id, data.account_id)
        if not record:
            raise ACMEException(status_code=status.HTTP_404_NOT_FOUND, type="malformed", detail='specified order not found for current account')
        order_status, expires_at, err = record
        authzs = [row async for row in sql('select id, domain from authorizations where order_id = $1', order_id)]
        cert_record = await sql.record('select serial_number, not_valid_before, not_valid_after from certificates where order_id = $1', order_id)
    if cert_record:
        cert_sn, not_valid_before, not_valid_after = cert_record
    if err:
        acme_error = ACMEException(type=err.type, detail=err.detail)
    else:
        acme_error = None
    return order_response(
        status=order_status, expires_at=expires_at, domains=[domain for _, domain in authzs], 
        authz_ids=[authz_id for authz_id, _ in authzs], order_id=order_id, 
        not_valid_before=not_valid_before if cert_record else None, not_valid_after=not_valid_after if cert_record else None,
        cert_serial_number=cert_sn if cert_record else None, error=acme_error)


@api.post('/orders/{order_id}/finalize')
async def finalize_order(response: Response, order_id: str, data: Annotated[RequestData[FinalizeOrderPayload], Depends(SignedRequest(FinalizeOrderPayload))]):
    async with db.transaction(readonly=True) as sql:
        record = await sql.record('''
            select status, expires_at, expires_at <= now() as is_expired from orders ord 
            where ord.id = $1 and ord.account_id = $2
        ''', order_id, data.account_id)
    if not record:
        raise ACMEException(status_code=status.HTTP_404_NOT_FOUND, type='malformed', detail='Unknown order for specified account.')
    order_status, expires_at, is_expired = record
    if order_status != 'ready':
        raise ACMEException(status_code=status.HTTP_403_FORBIDDEN, type='orderNotReady', detail=f'order status is: {order_status}')
    if is_expired:
        async with db.transaction() as sql:
            await sql.exec("""
                update orders set status='invalid', error=row('unauthorized','order expired') where id = $1 and status <> 'invalid'
            """, order_id)
            await sql.exec("update authorizations set status='expired' where order_id = $1", order_id)
        raise ACMEException(status_code=status.HTTP_403_FORBIDDEN, type='orderNotReady', detail='order expired')
    else:
        async with db.transaction() as sql:
            await sql.exec("update orders set status='processing' where id = $1 and status = 'ready'", order_id)
    
    async with db.transaction(readonly=True) as sql:
        records = [(authz_id, domain) async for authz_id, domain, *_ in sql("""
            select id, domain from authorizations where order_id = $1 and status = 'valid'
        """, order_id)]
    domains = [domain for authz_id, domain in records]
    authz_ids = [authz_id for authz_id, domain in records]

    csr_bytes = base64url_decode(data.payload.csr)

    csr, csr_pem, subject_domain, san_domains = await check_csr(csr_bytes, ordered_domains=domains)

    try:
        signed_cert = await ca_service.sign_csr(csr, subject_domain, san_domains)
        err = False
    except ACMEException as e:
        err = e
    except Exception as e:
        err = ACMEException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, type='serverInternal', detail=str(e))
        logger.warn('sign csr failed (account: %s)', data.account_id, exc_info=True)


    if err is False:
        cert_sn = SerialNumberConverter.int2hex(signed_cert.cert.serial_number)

        async with db.transaction() as sql:
            not_valid_before, not_valid_after = await sql.record('''
                insert into certificates (serial_number, csr_pem, chain_pem, order_id, not_valid_before, not_valid_after)
                values ($1, $2, $3, $4, $5, $6) returning not_valid_before, not_valid_after
            ''', cert_sn, csr_pem, signed_cert.cert_chain_pem, order_id, signed_cert.cert.not_valid_before, signed_cert.cert.not_valid_after)
            order_status = await sql.value('''
                update orders set status='valid' where id = $1 and status='processing' returning status
            ''', order_id)
    else:
        cert_sn = not_valid_before = not_valid_after = None
        async with db.transaction() as sql:
            order_status = await sql.value('''
                update orders set status='invalid', error=row($2,$3) where id = $1 returning status
            ''', order_id, err.type, err.detail_text)

    return order_response(
        status=order_status, expires_at=expires_at, domains=domains, authz_ids=authz_ids, order_id=order_id, 
        not_valid_before=not_valid_before, not_valid_after=not_valid_after, cert_serial_number=cert_sn, error=err
    )
    