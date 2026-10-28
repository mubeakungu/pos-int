"""
KRA eTIMS adapter (OSCU / online sales control unit API).

On every completed sale it sends a sales transaction to eTIMS, and KRA
returns a receipt signature. The signature and a verification link are put
on the receipt (as the "KRA invoice" number and QR code). Refunds are sent as
credit notes (receipt type "R") that point back at the original invoice.

Before going live a business must: register for eTIMS on the KRA portal,
apply for OSCU API access, then initialise the device once (the admin page has
an "Initialise device" button) to get the CMC key. Items must also be
registered on eTIMS with the same item codes this adapter sends.

Docs: https://www.kra.go.ke/online-services/etims (OSCU technical spec)
"""
import datetime
import hashlib

from .base import Integration, Field, Result, SALE_COMPLETED, SALE_REFUNDED
from .registry import register

BASE_URLS = {
    'sandbox': 'https://etims-api-sbx.kra.go.ke/etims-api',
    'production': 'https://etims-api.kra.go.ke/etims-api',
}

# eTIMS tax types: A exempt, B standard VAT 16%, C zero-rated, D non-VAT, E 8%
TAX_RATES = {'A': 0.0, 'B': 16.0, 'C': 0.0, 'D': 0.0, 'E': 8.0}

# eTIMS payment type codes
PAYMENT_TYPES = {'cash': '01', 'credit': '02', 'card': '05', 'mpesa': '06', 'mobile': '06', 'split': '07'}


def _eat_now():
    return datetime.datetime.utcnow() + datetime.timedelta(hours=3)


def split_tax(amount_incl, tax_type):
    """Split a VAT-inclusive amount into (taxable amount, tax)."""
    rate = TAX_RATES.get(tax_type, 0.0)
    tax = round(amount_incl * rate / (100 + rate), 2) if rate else 0.0
    return round(amount_incl, 2), tax


@register
class KraEtimsIntegration(Integration):
    adapter = 'kra_etims'
    name = 'KRA eTIMS'
    description = 'Sends every sale and refund to KRA eTIMS and prints the signed invoice number and QR code on the receipt.'
    category = 'tax'
    events = (SALE_COMPLETED, SALE_REFUNDED)
    fields = (
        Field('mode', 'Mode', default='mock', type='choice', choices=('mock', 'sandbox', 'production'),
              help='mock signs invoices locally so you can try the flow; sandbox uses KRA test servers.'),
        Field('tin', 'KRA PIN of the business', default='P000000000A', env='ETIMS_TIN'),
        Field('bhf_id', 'Branch ID', default='00', env='ETIMS_BHF_ID', help='00 is the head office.'),
        Field('device_serial', 'Device serial number', default='', env='ETIMS_DEVICE_SERIAL',
              help='The serial number you registered for OSCU on the KRA portal.'),
        Field('default_tax_type', 'Default tax type', default='B', type='choice', choices=('A', 'B', 'C', 'D', 'E'),
              help='Prices are treated as VAT-inclusive. B = 16% VAT, E = 8%, A = exempt, C = zero-rated, D = non-VAT.'),
        Field('item_class_code', 'Default item classification code', default='5020230100',
              help='UNSPSC-based class code registered with eTIMS for your items.'),
        Field('item_code_prefix', 'Item code prefix', default='KE1NTXU',
              help='Item codes are this prefix plus the product ID padded to 7 digits; register them on eTIMS first.'),
        Field('qty_unit', 'Quantity unit code', default='U'),
        Field('pkg_unit', 'Packaging unit code', default='NT'),
        Field('refund_invoice_offset', 'Refund invoice number offset', default='900000000',
              help='Credit notes need their own invoice number; the sale ID plus this offset is used.'),
        Field('qr_url_template', 'Receipt verification URL', default='https://etims-sbx.kra.go.ke/common/link/etims/receipt/indexEtimsReceiptData?Data={tin}{bhf_id}{signature}',
              help='Placeholders: {tin} {bhf_id} {signature} {invoice}'),
        Field('cmc_key', 'CMC key', secret=True, env='ETIMS_CMC_KEY',
              help='Returned once when you initialise the device; store it in this environment variable.'),
    )

    @property
    def base_url(self):
        return BASE_URLS.get(self.mode, BASE_URLS['sandbox'])

    def _headers(self):
        return {
            'tin': self.setting('tin'),
            'bhfId': self.setting('bhf_id'),
            'cmcKey': self.setting('cmc_key') or '',
            'Content-Type': 'application/json',
        }

    def item_code(self, product_id):
        return f"{self.setting('item_code_prefix')}{int(product_id or 0):07d}"

    # --- payload building ----------------------------------------------
    def build_sales_payload(self, sale, refund=False):
        now = _eat_now()
        sale_date = sale.get('timestamp_eat') or now
        items = sale.get('items') or []
        subtotal = sum(float(i.get('subtotal', 0)) for i in items) or 0.0
        discount = float(sale.get('discount') or 0)
        dc_rate = round(discount / subtotal * 100, 4) if subtotal and discount else 0.0
        default_tax = self.setting('default_tax_type') or 'B'

        totals = {t: {'taxable': 0.0, 'tax': 0.0} for t in TAX_RATES}
        item_list = []
        for seq, item in enumerate(items, start=1):
            qty = float(item.get('quantity', 1))
            price = float(item.get('price', 0))
            supply = round(price * qty, 2)
            dc_amt = round(supply * dc_rate / 100, 2)
            tax_type = item.get('tax_type') or default_tax
            taxable, tax = split_tax(supply - dc_amt, tax_type)
            totals[tax_type]['taxable'] += taxable
            totals[tax_type]['tax'] += tax
            item_list.append({
                'itemSeq': seq,
                'itemCd': self.item_code(item.get('id')),
                'itemClsCd': item.get('item_class_code') or self.setting('item_class_code'),
                'itemNm': str(item.get('name', ''))[:200],
                'bcd': item.get('barcode'),
                'pkgUnitCd': self.setting('pkg_unit'),
                'pkg': qty,
                'qtyUnitCd': self.setting('qty_unit'),
                'qty': qty,
                'prc': price,
                'splyAmt': supply,
                'dcRt': dc_rate,
                'dcAmt': dc_amt,
                'isrccCd': None, 'isrccNm': None, 'isrcRt': None, 'isrcAmt': None,
                'taxTyCd': tax_type,
                'taxblAmt': taxable,
                'taxAmt': tax,
                'totAmt': taxable,
            })

        invoice_no = int(sale['id'])
        if refund:
            invoice_no += int(self.setting('refund_invoice_offset') or 900000000)
        cashier = (sale.get('cashier') or 'admin')[:20]
        tot_taxable = round(sum(v['taxable'] for v in totals.values()), 2)
        tot_tax = round(sum(v['tax'] for v in totals.values()), 2)

        payload = {
            'invcNo': invoice_no,
            'orgInvcNo': int(sale['id']) if refund else 0,
            'custTin': sale.get('customer_pin') or None,
            'custNm': sale.get('customer_name') or None,
            'salesTyCd': 'N',
            'rcptTyCd': 'R' if refund else 'S',
            'pmtTyCd': PAYMENT_TYPES.get((sale.get('payment_method') or 'cash').lower(), '01'),
            'salesSttsCd': '02',
            'cfmDt': now.strftime('%Y%m%d%H%M%S'),
            'salesDt': sale_date.strftime('%Y%m%d'),
            'stockRlsDt': now.strftime('%Y%m%d%H%M%S'),
            'cnclReqDt': None, 'cnclDt': None,
            'rfdDt': now.strftime('%Y%m%d%H%M%S') if refund else None,
            'rfdRsnCd': '06' if refund else None,  # 06 = refund (other reasons are in the KRA code list)
            'totItemCnt': len(item_list),
            'totTaxblAmt': tot_taxable,
            'totTaxAmt': tot_tax,
            'totAmt': tot_taxable,
            'prchrAcptcYn': 'N',
            'remark': sale.get('refund_reason') if refund else None,
            'regrId': cashier, 'regrNm': cashier, 'modrId': cashier, 'modrNm': cashier,
            'receipt': {
                'custTin': sale.get('customer_pin') or None,
                'custMblNo': sale.get('customer_phone') or None,
                'rptNo': invoice_no,
                'trdeNm': sale.get('business_name') or None,
                'adrs': sale.get('business_address') or None,
                'topMsg': sale.get('business_name') or None,
                'btmMsg': 'Thank you',
                'prchrAcptcYn': 'N',
            },
            'itemList': item_list,
        }
        for t, rate in TAX_RATES.items():
            payload[f'taxblAmt{t}'] = round(totals[t]['taxable'], 2)
            payload[f'taxRt{t}'] = rate
            payload[f'taxAmt{t}'] = round(totals[t]['tax'], 2)
        return payload

    def _fiscal(self, invoice_no, signature, data=None):
        url = (self.setting('qr_url_template') or '').format(
            tin=self.setting('tin'), bhf_id=self.setting('bhf_id'), signature=signature, invoice=invoice_no)
        return {
            'invoice_number': f"{self.setting('tin')}/{self.setting('bhf_id')}/{invoice_no}",
            'signature': signature,
            'qr_data': url,
            'receipt_data': data or {},
        }

    # --- events ---------------------------------------------------------
    def handle(self, event, payload):
        if event not in self.events:
            return Result.skip(f'Not handled: {event}')
        refund = event == SALE_REFUNDED
        sale = payload['sale']
        body = self.build_sales_payload(sale, refund=refund)

        if self.is_mock:
            signature = hashlib.sha256(f"{body['invcNo']}|{body['totAmt']}|{body['cfmDt']}".encode()).hexdigest()[:16].upper()
            data = {'rcptNo': body['invcNo'], 'rcptSign': signature, 'sdcId': 'MOCK-SDC', 'mrcNo': 'MOCK-MRC', 'mock': True}
            return Result.success({'fiscal': self._fiscal(body['invcNo'], signature, data)}, request=body,
                                  response={'resultCd': '000', 'resultMsg': 'Mock', 'data': data})

        missing = self.missing_settings()
        if missing:
            return Result.failure(f'Missing eTIMS settings: {", ".join(missing)}', request=body)
        try:
            resp = self.http.post(f'{self.base_url}/saveTrnsSalesOsdc', json=body, headers=self._headers(), timeout=30)
        except Exception as e:
            return Result.failure(f'Could not reach eTIMS: {e}', request=body)
        data = self._json(resp)
        if not resp.ok or data.get('resultCd') != '000':
            return Result.failure(f"eTIMS rejected the invoice: {data.get('resultMsg') or resp.status_code}",
                                  request=body, response=data)
        rcpt = data.get('data') or {}
        return Result.success({'fiscal': self._fiscal(body['invcNo'], rcpt.get('rcptSign', ''), rcpt)},
                              request=body, response=data)

    def initialize_device(self):
        """One-time OSCU initialisation that returns the CMC key."""
        body = {'tin': self.setting('tin'), 'bhfId': self.setting('bhf_id'), 'dvcSrlNo': self.setting('device_serial')}
        if self.is_mock:
            return Result.success({'cmc_key': 'MOCK-CMC-KEY', 'message': 'Mock device initialised.'}, request=body)
        try:
            resp = self.http.post(f'{self.base_url}/selectInitOsdcInfo', json=body,
                                  headers={'Content-Type': 'application/json'}, timeout=30)
        except Exception as e:
            return Result.failure(f'Could not reach eTIMS: {e}', request=body)
        data = self._json(resp)
        if data.get('resultCd') not in ('000', '902'):  # 902 = device already installed
            return Result.failure(f"eTIMS: {data.get('resultMsg') or resp.status_code}", request=body, response=data)
        info = (data.get('data') or {}).get('info') or {}
        return Result.success({'cmc_key': info.get('cmcKey'), 'message': data.get('resultMsg')}, request=body,
                              response={k: v for k, v in data.items() if k != 'data'})

    def test_connection(self):
        if self.is_mock:
            return Result.success({'message': 'Mock mode: invoices are signed locally. Switch to sandbox once KRA gives you OSCU access.'})
        missing = self.missing_settings()
        if missing:
            return Result.failure(f'Set these environment variables first: {", ".join(missing)}')
        body = {'lastReqDt': '20200101000000'}
        try:
            resp = self.http.post(f'{self.base_url}/selectCodeList', json=body, headers=self._headers(), timeout=30)
        except Exception as e:
            return Result.failure(f'Could not reach eTIMS: {e}')
        data = self._json(resp)
        if data.get('resultCd') in ('000', '001'):  # 001 = no new data, still a valid auth
            return Result.success({'message': f'Connected to eTIMS ({self.mode}).'})
        return Result.failure(f"eTIMS: {data.get('resultMsg') or resp.status_code}")
