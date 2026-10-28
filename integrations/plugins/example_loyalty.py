"""
Example plugin: award loyalty points through a (hypothetical) loyalty API.

Copy this file, change the adapter id, fields and handle() logic, and the new
integration appears on the Integrations page. Nothing else needs editing.
"""
from integrations.base import Integration, Field, Result, SALE_COMPLETED, SALE_REFUNDED


class LoyaltyPointsIntegration(Integration):
    adapter = 'example_loyalty'
    name = 'Loyalty points (example plugin)'
    description = 'Sends each sale to a loyalty API so customers earn points. A template for writing your own adapter.'
    category = 'other'
    events = (SALE_COMPLETED, SALE_REFUNDED)
    fields = (
        Field('mode', 'Mode', default='mock', type='choice', choices=('mock', 'live')),
        Field('base_url', 'API base URL', default='https://loyalty.example.com/api'),
        Field('points_per_100', 'Points per 100 spent', default='1'),
        Field('api_key', 'API key', secret=True, env='LOYALTY_API_KEY'),
    )

    def handle(self, event, payload):
        sale = payload['sale']
        phone = sale.get('customer_phone')
        if not phone:
            return Result.skip('Sale has no customer phone, so no points to award.')

        points = int(sale['total'] // 100 * int(self.setting('points_per_100') or 1))
        if event == SALE_REFUNDED:
            points = -points
        body = {'phone': phone, 'points': points, 'reference': f"SALE{sale['id']}"}

        if self.is_mock:
            return Result.success({'points': points, 'mock': True}, request=body)

        resp = self.http.post(
            f"{self.setting('base_url').rstrip('/')}/points",
            json=body,
            headers={'Authorization': f"Bearer {self.setting('api_key')}"},
            timeout=15,
        )
        data = self._json(resp)
        if not resp.ok:
            return Result.failure(f'Loyalty API returned {resp.status_code}', request=body, response=data)
        return Result.success({'points': points}, request=body, response=data)
