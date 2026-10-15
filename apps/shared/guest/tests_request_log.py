from django.test import SimpleTestCase, RequestFactory
from unittest import mock

from .request_log import ClientLogView, GuestRequestLogMiddleware


class GuestRequestLogTests(SimpleTestCase):
    def test_request_line(self):
        rf = RequestFactory()
        req = rf.get('/api/v1/branches/1/', HTTP_ORIGIN='https://vkapp.levelupapp.ru', HTTP_X_WEB_SESSION='t')
        req.vk_sign_status = 'web'
        mw = GuestRequestLogMiddleware(lambda r: mock.Mock(status_code=200))
        with self.assertLogs('guest.requests', 'INFO') as cm:
            mw(req)
        line = cm.output[0]
        self.assertIn('GET /api/v1/branches/1/ 200', line)
        self.assertIn('sign=web web=yes tg=no origin=https://vkapp.levelupapp.ru', line)
        self.assertNotIn("'t'", line)

    def test_non_api_not_logged(self):
        req = RequestFactory().get('/admin/')
        mw = GuestRequestLogMiddleware(lambda r: mock.Mock(status_code=200))
        with self.assertNoLogs('guest.requests', 'INFO'):
            mw(req)

    def test_client_log(self):
        req = RequestFactory().post('/api/v1/client-log/', {'stage': 'vkid_exchange', 'message': 'x' * 900},
                                    content_type='application/json')
        with self.assertLogs('guest.client', 'WARNING') as cm:
            resp = ClientLogView.as_view()(req)
        self.assertEqual(resp.status_code, 204)
        self.assertIn('stage=vkid_exchange', cm.output[0])
        self.assertLess(len(cm.output[0]), 1500)
