"""База знаний по назначению (15.10.2026): ответы на отзывы и посты получают свои документы."""
from django.test import SimpleTestCase

from apps.tenant.analytics.ai_service import kb_purpose


class KbPurposeTest(SimpleTestCase):
    def test_levone_documents(self):
        self.assertEqual(kb_purpose('База знаний. Сегментация отзывов', ''), 'reviews')
        self.assertEqual(kb_purpose('ОБЯЗАТЕЛЬНОЕ ПРАВИЛО ОТВЕТА НА ОТЗЫВЫ', ''), 'reviews')
        self.assertEqual(kb_purpose('Рассылки', ''), 'content')
        self.assertEqual(kb_purpose('База знаний для контент-плана 1', ''), 'content')
        self.assertEqual(kb_purpose('Меню', 'Для постоянных гостей: ...'), 'both')

    def test_text_marks_content(self):
        self.assertEqual(kb_purpose('Часть 1', 'Часть 1 из 2 базы знаний ИИ маркетолога'), 'content')
