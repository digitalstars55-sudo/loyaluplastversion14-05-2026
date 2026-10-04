"""Encrypt legacy integration values; never print their contents."""
from django.apps import apps
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django_tenants.utils import schema_context, get_tenant_model

from apps.shared.secret_fields import EncryptedSecretField, PREFIX, cipher


def encrypt_model(model, commit=False):
    quote = connection.ops.quote_name
    table = quote(model._meta.db_table)
    pk = quote(model._meta.pk.column)
    result = {'plaintext': 0, 'encrypted': 0, 'updated': 0}
    for field in model._meta.fields:
        if not isinstance(field, EncryptedSecretField):
            continue
        col = quote(field.column)
        with connection.cursor() as cursor:
            cursor.execute(f'SELECT {pk}, {col} FROM {table} WHERE {col} IS NOT NULL AND {col} != %s', [''])
            rows = cursor.fetchall()
            for row_pk, value in rows:
                if value.startswith(PREFIX):
                    field.from_db_value(value, None, connection)  # wrong key must fail loudly
                    result['encrypted'] += 1
                    continue
                result['plaintext'] += 1
                if commit:
                    stored = field.get_prep_value(value)
                    # Do not overwrite a concurrent credentials edit.
                    cursor.execute(f'UPDATE {table} SET {col}=%s WHERE {pk}=%s AND {col}=%s',
                                   [stored, row_pk, value])
                    result['updated'] += cursor.rowcount
    return result


class Command(BaseCommand):
    help = 'Encrypt legacy integration secrets in public and tenant schemas; dry run by default.'

    def add_arguments(self, parser):
        parser.add_argument('--commit', action='store_true')
        parser.add_argument('--schema', help='Only this schema; default is all configured schemas.')

    def handle(self, *args, **options):
        commit = options['commit']
        if commit:
            cipher()  # verify configured key before the first write
        with schema_context('public'):
            tenants = list(get_tenant_model().objects.exclude(schema_name='public').values_list('schema_name', flat=True))
        schemas = ['public', *tenants]
        if options.get('schema'):
            if options['schema'] not in schemas:
                raise CommandError('Unknown schema')
            schemas = [options['schema']]
        names = {
            'public': [('config', 'ClientConfig')],
            'tenant': [('senler', 'SenlerConfig'), ('telegram', 'TelegramBot'), ('marketer', 'MarketerSettings')],
        }
        total_plain = total_updated = 0
        for schema in schemas:
            with schema_context(schema), transaction.atomic():
                for label, name in names['public' if schema == 'public' else 'tenant']:
                    stats = encrypt_model(apps.get_model(label, name), commit=commit)
                    total_plain += stats['plaintext']
                    total_updated += stats['updated']
                    self.stdout.write(f'{schema}.{label}.{name}: {stats}')
        self.stdout.write(f'commit={commit} plaintext_before={total_plain} updated={total_updated}')
