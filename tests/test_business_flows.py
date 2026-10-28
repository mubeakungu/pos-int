import datetime


def add_customer(client, m, **kw):
    data = {'name': 'Acme Ltd', 'phone': '0722000111', 'email': 'acme@example.com', 'kra_pin': 'p051234567x'}
    data.update(kw)
    client.post('/customers', data=data)
    return m.Customer.query.filter_by(name=data['name']).one()


def doc_form(customer_id, lines, **extra):
    data = {'customer_id': customer_id, 'issue_date': datetime.date.today().isoformat(),
            'line_product_id': [], 'line_description': [], 'line_quantity': [], 'line_price': [], 'line_tax_type': []}
    for pid, desc, qty, price, tax in lines:
        data['line_product_id'].append(pid)
        data['line_description'].append(desc)
        data['line_quantity'].append(qty)
        data['line_price'].append(price)
        data['line_tax_type'].append(tax)
    data.update(extra)
    return data


def test_all_new_pages_render(client, m):
    for url in ('/customers', '/documents?type=quotation', '/documents?type=proforma', '/documents?type=invoice',
                '/documents/new?type=quotation', '/documents/new?type=invoice', '/shift', '/reports/end-of-day',
                '/reports/debtors', '/suppliers', '/purchase-orders', '/purchase-orders/new', '/sales', '/', '/reports',
                '/products', '/products/edit/1', '/settings'):
        resp = client.get(url)
        assert resp.status_code == 200, url


def test_quotation_to_invoice_with_payments_and_statement(client, m):
    customer = add_customer(client, m)
    assert customer.kra_pin == 'P051234567X'
    resp = client.post('/documents/new?type=quotation', data=doc_form(customer.id, [
        ('1', '', '3', '', 'B'),                       # Bread from stock, price filled from product
        ('', 'Delivery to Westlands', '1', '500', 'B'),  # free-text service line
    ], discount_amount='45', type='quotation'))
    quote = m.Document.query.filter_by(doc_type='quotation').one()
    assert resp.location.endswith(f'/documents/{quote.id}')
    assert quote.number == 'QUO-0001' and quote.status == 'draft'
    assert quote.subtotal == 3 * 65 + 500 and quote.total == 650.0
    assert quote.total_tax == round(650 * 16 / 116, 2)
    assert client.get(f'/documents/{quote.id}').status_code == 200

    client.post(f'/documents/{quote.id}/status', data={'status': 'accepted'})
    client.post(f'/documents/{quote.id}/convert/proforma')
    proforma = m.Document.query.filter_by(doc_type='proforma').one()
    assert proforma.number == 'PRO-0001' and proforma.source_id == quote.id
    m.db.session.refresh(quote)
    assert quote.status == 'converted'

    client.post(f'/documents/{proforma.id}/convert/invoice')
    invoice = m.Document.query.filter_by(doc_type='invoice').one()
    assert invoice.number == 'INV-0001' and invoice.due_date > datetime.date.today()

    client.post(f'/documents/{invoice.id}/issue')
    m.db.session.refresh(invoice)
    assert invoice.status == 'unpaid'
    assert m.Product.query.get(1).quantity == 7
    assert invoice.sale.kra_invoice_id  # sent to eTIMS (mock)
    assert invoice.sale.meta.payment_method == 'credit'

    client.post(f'/documents/{invoice.id}/payments', data={'amount': '200', 'method': 'mpesa', 'reference': 'QAB1'})
    m.db.session.refresh(invoice)
    assert invoice.status == 'partial' and invoice.balance == 450.0
    # overpaying is refused
    client.post(f'/documents/{invoice.id}/payments', data={'amount': '1000', 'method': 'cash'})
    assert invoice.balance == 450.0

    statement = client.get(f'/customers/{customer.id}/statement')
    assert b'INV-0001' in statement.data and b'450.00' in statement.data
    assert b'Acme Ltd' in client.get('/reports/debtors').data

    client.post(f'/documents/{invoice.id}/payments', data={'amount': '450', 'method': 'bank'})
    m.db.session.refresh(invoice)
    assert invoice.status == 'paid' and invoice.balance == 0


def test_issue_refuses_when_stock_is_short(client, m):
    client.post('/documents/new?type=invoice', data=doc_form('', [('1', '', '50', '', 'B')], type='invoice'))
    invoice = m.Document.query.one()
    client.post(f'/documents/{invoice.id}/issue')
    m.db.session.refresh(invoice)
    assert invoice.status == 'draft' and m.Product.query.get(1).quantity == 10


def test_void_invoice_restocks_and_sends_credit_note(client, m):
    client.post('/documents/new?type=invoice', data=doc_form('', [('2', '', '4', '', 'B')], type='invoice'))
    invoice = m.Document.query.one()
    client.post(f'/documents/{invoice.id}/issue')
    assert m.Product.query.get(2).quantity == 6
    client.post(f'/documents/{invoice.id}/void', data={'reason': 'Order cancelled'})
    m.db.session.refresh(invoice)
    assert invoice.status == 'void' and m.Product.query.get(2).quantity == 10
    assert m.IntegrationLog.query.filter_by(event='sale.refunded').count() == 1
    # invoice sales can't be refunded from the receipt
    client.post(f'/receipt/{invoice.sale_id}/refund')
    assert m.IntegrationLog.query.filter_by(event='sale.refunded').count() == 1


def test_quotation_sold_at_till(client, m):
    client.post('/documents/new?type=quotation', data=doc_form('', [('1', '', '2', '60', 'B')], type='quotation'))
    quote = m.Document.query.one()
    client.post(f'/documents/{quote.id}/to-cart')
    client.post('/complete_sale', data={'payment_method': 'cash'})
    sale = m.Sale.query.one()
    m.db.session.refresh(quote)
    assert sale.total_amount == 120.0 and quote.status == 'converted' and quote.sale_id == sale.id


def test_checkout_saves_customer_and_change(client, m):
    client.post('/sales', data={'product_id': 1, 'quantity': 2})
    client.post('/complete_sale', data={'payment_method': 'cash', 'customer_name': 'Jane', 'customer_phone': '0711000222',
                                        'amount_tendered': '200'})
    sale = m.Sale.query.one()
    assert sale.meta.customer.name == 'Jane'
    assert sale.payments[0].message == 'Tendered 200.00, change 70.00'
    assert b'change 70.00' in client.get(f'/view_receipt/{sale.id}').data
    # tendering too little is refused
    client.post('/sales', data={'product_id': 1, 'quantity': 1})
    client.post('/complete_sale', data={'payment_method': 'cash', 'amount_tendered': '10'})
    assert m.Sale.query.count() == 1


def test_split_payment_cash_plus_mpesa(client, m):
    config = m.IntegrationConfig.query.filter_by(key='mpesa').one()
    config.enabled = True
    m.db.session.commit()
    client.post('/sales', data={'product_id': 2, 'quantity': 3})  # 210
    client.post('/complete_sale', data={'payment_method': 'mpesa', 'payment_phone': '0712345678', 'cash_part': '60'})
    sale = m.Sale.query.one()
    cash, mpesa = sale.payments
    assert (cash.method, cash.amount, cash.status) == ('cash', 60.0, 'paid')
    assert (mpesa.method, mpesa.amount, mpesa.status) == ('mpesa', 150.0, 'pending')
    assert sale.kra_invoice_id is None
    client.post(f'/payments/{mpesa.id}/simulate', data={'outcome': 'paid'})
    m.db.session.refresh(sale)
    assert sale.kra_invoice_id is not None


def test_shift_z_report(client, m):
    client.post('/shift', data={'action': 'open', 'opening_float': '1000'})
    shift = m.Shift.query.one()
    client.post('/sales', data={'product_id': 1, 'quantity': 2})  # 130 cash
    client.post('/complete_sale', data={'payment_method': 'cash'})
    client.post('/sales', data={'product_id': 2, 'quantity': 1})  # 70 cash, refunded
    client.post('/complete_sale', data={'payment_method': 'cash'})
    refunded = m.Sale.query.order_by(m.Sale.id.desc()).first()
    client.post(f'/receipt/{refunded.id}/refund')
    summary = m.shift_summary(shift)
    assert summary['transactions'] == 2 and summary['by_method'] == {'cash': 200.0}
    assert summary['refunds'] == 70.0 and summary['expected_cash'] == 1130.0
    client.post('/shift', data={'action': 'close', 'counted_cash': '1120'})
    m.db.session.refresh(shift)
    assert not shift.is_open and shift.expected_cash == 1130.0
    report = client.get(f'/shift/{shift.id}/report')
    assert b'Short' in report.data and b'-10.00' in report.data


def test_purchase_order_receives_stock_and_cost(client, m):
    client.post('/suppliers', data={'name': 'Unga Distributors', 'phone': '0700111222'})
    supplier = m.Supplier.query.one()
    client.post('/purchase-orders/new', data={'supplier_id': supplier.id, 'line_product_id': ['1', '2'],
                                              'line_quantity': ['24', '12'], 'line_unit_cost': ['50', '55']})
    po = m.PurchaseOrder.query.one()
    assert po.total == 24 * 50 + 12 * 55
    assert client.get(f'/purchase-orders/{po.id}').status_code == 200
    client.post(f'/purchase-orders/{po.id}/receive', data={'received_0': '24', 'received_1': '10', 'supplier_invoice_no': 'UD-77'})
    m.db.session.refresh(po)
    assert po.status == 'received'
    bread, milk = m.Product.query.get(1), m.Product.query.get(2)
    assert (bread.quantity, bread.cost_price) == (34, 50.0)
    assert (milk.quantity, milk.cost_price) == (20, 55.0)


def test_product_category_and_vat_flow_into_etims(client, m):
    client.post('/products', data={'name': 'Eggs tray', 'price': '450', 'quantity': '10', 'category': 'Dairy',
                                   'tax_type': 'A', 'cost_price': '380'})
    eggs = m.Product.query.filter_by(name='Eggs tray').one()
    assert (eggs.category, eggs.tax_type, eggs.cost_price) == ('Dairy', 'A', 380.0)
    client.post('/sales', data={'product_id': eggs.id, 'quantity': 1})
    client.post('/complete_sale', data={'payment_method': 'cash'})
    import json
    log = m.IntegrationLog.query.filter_by(event='sale.completed').one()
    body = json.loads(log.request_json)
    assert body['itemList'][0]['taxTyCd'] == 'A' and body['totTaxAmt'] == 0
