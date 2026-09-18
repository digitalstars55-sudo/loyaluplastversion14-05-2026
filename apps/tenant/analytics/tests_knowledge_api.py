"""
База знаний ИИ (контракт 3б.7, №52). Модель тенантная — менеджер подменяем.

Главное, что проверяем: DELETE архивирует, а не удаляет (документ участвовал в
анализе тысяч отзывов), и загрузка чужих форматов отбивается — PDF дал бы
документ с пустым текстом, и ИИ «читал» бы пустоту.
"""
from datetime import datetime, timezone as dt_timezone
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase
from rest_framework.permissions import IsAuthenticated
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.tenant.analytics.api import knowledge as K

KP = 'apps.tenant.analytics.api.knowledge.'


def _doc(pk=1, title='Инструкция', is_active=True, text='правила анализа', name='knowledge_base/a.docx'):
    doc = SimpleNamespace(pk=pk, title=title, is_active=is_active, extracted_text=text,
                          file=SimpleNamespace(name=name),
                          created_at=datetime(2026, 9, 18, tzinfo=dt_timezone.utc),
                          updated_at=datetime(2026, 9, 18, tzinfo=dt_timezone.utc))
    doc.save = MagicMock()
    doc.delete = MagicMock()
    doc.refresh_from_db = MagicMock()
    return doc


def _user(role='network_admin'):
    return SimpleNamespace(is_authenticated=True, is_active=True, is_superuser=False,
                           is_superadmin=False, is_network_admin=(role == 'network_admin'),
                           is_client=(role == 'client'), username='alina', pk=1, is_staff=True)


def _call(view_cls, method, path, *, data=None, params=None, user=None, fmt='json', **kwargs):
    factory = APIRequestFactory()
    if method == 'get':
        request = factory.get(path, params or {})
    elif method == 'delete':
        request = factory.delete(path)
    elif fmt == 'multipart':
        request = factory.post(path, data or {}, format='multipart')
    else:
        request = getattr(factory, method)(path, data or {}, format='json')
    force_authenticate(request, user=user or _user())
    return view_cls.as_view()(request, **kwargs)


def _objects(rows=(), row=None):
    objects = MagicMock()
    qs = MagicMock()
    qs.filter.return_value = qs
    qs.count.return_value = len(rows)
    qs.__getitem__ = lambda self_, item: list(rows)
    objects.all.return_value.order_by.return_value = qs
    objects.filter.return_value.first.return_value = row
    return objects, qs


class ListTest(SimpleTestCase):

    def test_archived_are_hidden_by_default(self):
        objects, qs = _objects([_doc()])
        with patch.object(K.KnowledgeBaseDocument, 'objects', objects):
            resp = _call(K.KnowledgeListCreateAPIView, 'get', '/api/v1/ai/knowledge/')
        self.assertEqual(resp.status_code, 200)
        qs.filter.assert_called_once_with(is_active=True)
        doc = resp.data['documents'][0]
        self.assertEqual((doc['filename'], doc['has_text']), ('a.docx', True))
        self.assertEqual(doc['char_count'], len('правила анализа'))

    def test_include_archived(self):
        objects, qs = _objects([_doc(is_active=False)])
        with patch.object(K.KnowledgeBaseDocument, 'objects', objects):
            resp = _call(K.KnowledgeListCreateAPIView, 'get', '/api/v1/ai/knowledge/',
                         params={'include_archived': '1'})
        self.assertEqual(resp.status_code, 200)
        qs.filter.assert_not_called()

    def test_limit_is_capped(self):
        objects, _ = _objects([])
        with patch.object(K.KnowledgeBaseDocument, 'objects', objects):
            resp = _call(K.KnowledgeListCreateAPIView, 'get', '/api/v1/ai/knowledge/',
                         params={'limit': '9999'})
        self.assertEqual(resp.data['limit'], K.MAX_LIMIT)

    def test_requires_auth(self):
        for view in (K.KnowledgeListCreateAPIView, K.KnowledgeDetailAPIView, K.KnowledgeTextAPIView):
            self.assertIn(IsAuthenticated, view.permission_classes)


class UploadTest(SimpleTestCase):

    def _upload(self, filename, content=b'x', user=None):
        objects, _ = _objects([], row=None)
        created = _doc(title=filename, name=f'knowledge_base/{filename}')
        objects.create.return_value = created
        with patch.object(K.KnowledgeBaseDocument, 'objects', objects), \
             patch(KP + 'current_schema_name', return_value='levone'):
            resp = _call(K.KnowledgeListCreateAPIView, 'post', '/api/v1/ai/knowledge/',
                         data={'file': SimpleUploadedFile(filename, content), 'title': ''},
                         fmt='multipart', user=user)
        return resp, objects

    def test_docx_is_accepted(self):
        resp, objects = self._upload('instructions.docx')
        self.assertEqual(resp.status_code, 201)
        objects.create.assert_called_once()
        self.assertEqual(objects.create.call_args.kwargs['title'], 'instructions.docx',
                         'без названия берём имя файла')

    def test_txt_is_accepted(self):
        resp, _ = self._upload('rules.txt')
        self.assertEqual(resp.status_code, 201)

    def test_pdf_is_rejected(self):
        resp, objects = self._upload('menu.pdf')
        self.assertEqual((resp.status_code, resp.data['code']), (400, 'invalid_payload'))
        objects.create.assert_not_called()

    def test_too_big_is_rejected(self):
        big = SimpleUploadedFile('big.txt', b'x')
        big.size = K.MAX_FILE_BYTES + 1
        objects, _ = _objects([])
        with patch.object(K.KnowledgeBaseDocument, 'objects', objects):
            factory = APIRequestFactory()
            request = factory.post('/api/v1/ai/knowledge/', {'file': big}, format='multipart')
            force_authenticate(request, user=_user())
            resp = K.KnowledgeListCreateAPIView.as_view()(request)
        self.assertEqual((resp.status_code, resp.data['code']), (400, 'invalid_payload'))
        objects.create.assert_not_called()

    def test_without_file_is_400(self):
        objects, _ = _objects([])
        with patch.object(K.KnowledgeBaseDocument, 'objects', objects):
            resp = _call(K.KnowledgeListCreateAPIView, 'post', '/api/v1/ai/knowledge/',
                         data={'title': 'без файла'})
        self.assertEqual((resp.status_code, resp.data['code']), (400, 'invalid_payload'))

    def test_client_cannot_upload(self):
        resp, objects = self._upload('rules.txt', user=_user(role='client'))
        self.assertEqual((resp.status_code, resp.data['code']), (403, 'role_not_allowed'))
        objects.create.assert_not_called()


class PatchAndArchiveTest(SimpleTestCase):

    def test_patch_title_and_flag(self):
        doc = _doc()
        objects, _ = _objects([], row=doc)
        with patch.object(K.KnowledgeBaseDocument, 'objects', objects), \
             patch(KP + 'current_schema_name', return_value='levone'):
            resp = _call(K.KnowledgeDetailAPIView, 'patch', '/api/v1/ai/knowledge/1/',
                         data={'title': 'Новое имя', 'is_active': False}, pk=1)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(doc.title, 'Новое имя')
        self.assertFalse(doc.is_active)
        doc.save.assert_called_once_with(update_fields=['is_active', 'title', 'updated_at'])

    def test_foreign_field_is_rejected(self):
        objects, _ = _objects([], row=_doc())
        with patch.object(K.KnowledgeBaseDocument, 'objects', objects):
            resp = _call(K.KnowledgeDetailAPIView, 'patch', '/api/v1/ai/knowledge/1/',
                         data={'extracted_text': 'подменим текст'}, pk=1)
        self.assertEqual((resp.status_code, resp.data['code']), (400, 'invalid_payload'))
        self.assertEqual(resp.data['editable'], ['title', 'is_active'])

    def test_empty_title_is_rejected(self):
        objects, _ = _objects([], row=_doc())
        with patch.object(K.KnowledgeBaseDocument, 'objects', objects):
            resp = _call(K.KnowledgeDetailAPIView, 'patch', '/api/v1/ai/knowledge/1/',
                         data={'title': '  '}, pk=1)
        self.assertEqual((resp.status_code, resp.data['code']), (400, 'invalid_payload'))

    def test_delete_archives_and_keeps_the_file(self):
        doc = _doc()
        objects, _ = _objects([], row=doc)
        with patch.object(K.KnowledgeBaseDocument, 'objects', objects), \
             patch(KP + 'current_schema_name', return_value='levone'):
            resp = _call(K.KnowledgeDetailAPIView, 'delete', '/api/v1/ai/knowledge/1/', pk=1)
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(doc.is_active)
        doc.save.assert_called_once_with(update_fields=['is_active', 'updated_at'])
        doc.delete.assert_not_called()

    def test_delete_twice_is_harmless(self):
        doc = _doc(is_active=False)
        objects, _ = _objects([], row=doc)
        with patch.object(K.KnowledgeBaseDocument, 'objects', objects), \
             patch(KP + 'current_schema_name', return_value='levone'):
            resp = _call(K.KnowledgeDetailAPIView, 'delete', '/api/v1/ai/knowledge/1/', pk=1)
        self.assertEqual(resp.status_code, 204)
        doc.save.assert_not_called()

    def test_missing_is_404(self):
        objects, _ = _objects([], row=None)
        with patch.object(K.KnowledgeBaseDocument, 'objects', objects):
            resp = _call(K.KnowledgeDetailAPIView, 'patch', '/api/v1/ai/knowledge/9/',
                         data={'title': 'x'}, pk=9)
        self.assertEqual((resp.status_code, resp.data['code']), (404, 'not_found'))

    def test_client_cannot_archive(self):
        doc = _doc()
        objects, _ = _objects([], row=doc)
        with patch.object(K.KnowledgeBaseDocument, 'objects', objects):
            resp = _call(K.KnowledgeDetailAPIView, 'delete', '/api/v1/ai/knowledge/1/',
                         user=_user(role='client'), pk=1)
        self.assertEqual((resp.status_code, resp.data['code']), (403, 'role_not_allowed'))
        doc.save.assert_not_called()


class TextTest(SimpleTestCase):

    def test_text_is_truncated(self):
        doc = _doc(text='я' * (K.TEXT_LIMIT + 500))
        objects, _ = _objects([], row=doc)
        with patch.object(K.KnowledgeBaseDocument, 'objects', objects):
            resp = _call(K.KnowledgeTextAPIView, 'get', '/api/v1/ai/knowledge/1/text/', pk=1)
        self.assertEqual(len(resp.data['text']), K.TEXT_LIMIT)
        self.assertTrue(resp.data['truncated'])
        self.assertEqual(resp.data['char_count'], K.TEXT_LIMIT + 500)

    def test_short_text_is_not_truncated(self):
        objects, _ = _objects([], row=_doc(text='коротко'))
        with patch.object(K.KnowledgeBaseDocument, 'objects', objects):
            resp = _call(K.KnowledgeTextAPIView, 'get', '/api/v1/ai/knowledge/1/text/', pk=1)
        self.assertFalse(resp.data['truncated'])
        self.assertEqual(resp.data['text'], 'коротко')

    def test_client_can_read(self):
        objects, _ = _objects([], row=_doc())
        with patch.object(K.KnowledgeBaseDocument, 'objects', objects):
            resp = _call(K.KnowledgeTextAPIView, 'get', '/api/v1/ai/knowledge/1/text/',
                         user=_user(role='client'), pk=1)
        self.assertEqual(resp.status_code, 200)
