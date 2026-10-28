import os
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_db_dir = tempfile.mkdtemp()
os.environ['DATABASE_URL'] = f"sqlite:///{os.path.join(_db_dir, 'test.db')}"
os.environ.setdefault('SECRET_KEY', 'test-secret')
os.environ.setdefault('ADMIN_PASSWORD', 'admin123')
for var in ('MPESA_CONSUMER_KEY', 'MPESA_CONSUMER_SECRET', 'MPESA_PASSKEY', 'MPESA_CALLBACK_TOKEN', 'ETIMS_CMC_KEY'):
    os.environ.pop(var, None)

import app as app_module  # noqa: E402


@pytest.fixture
def app():
    flask_app = app_module.app
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with flask_app.app_context():
        app_module.db.drop_all()
        app_module.db.create_all()
        admin = app_module.User(username='admin', password='admin123', role='admin')
        app_module.db.session.add(admin)
        app_module.db.session.add(app_module.Product(name='Bread', price=65.0, quantity=10, barcode='111'))
        app_module.db.session.add(app_module.Product(name='Milk', price=70.0, quantity=10))
        app_module.db.session.commit()
        app_module.seed_default_integrations()
        yield flask_app
        app_module.db.session.remove()


@pytest.fixture
def client(app):
    c = app.test_client()
    c.post('/login', data={'username': 'admin', 'password': 'admin123'})
    return c


@pytest.fixture
def m():
    return app_module


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = str(self._payload)

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    def json(self):
        return self._payload


class FakeHttp:
    """Stands in for requests.Session: records calls and returns queued responses."""
    def __init__(self, responses=None):
        self.calls = []
        self.responses = list(responses or [])

    def _next(self, method, url, **kwargs):
        self.calls.append({'method': method, 'url': url, **kwargs})
        return self.responses.pop(0) if self.responses else FakeResponse(200, {})

    def get(self, url, **kwargs):
        return self._next('GET', url, **kwargs)

    def post(self, url, **kwargs):
        return self._next('POST', url, **kwargs)

    def request(self, method, url, **kwargs):
        return self._next(method, url, **kwargs)


@pytest.fixture
def fake_http():
    return FakeHttp
