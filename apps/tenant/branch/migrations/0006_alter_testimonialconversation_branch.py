import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('branch', '0005_testimonialconversation_vk_guest'),
    ]

    operations = [
        # Step 1: drop NOT NULL so we can set orphaned rows to NULL
        migrations.RunSQL(
            sql="ALTER TABLE branch_testimonialconversation ALTER COLUMN branch_id DROP NOT NULL;",
            reverse_sql=migrations.RunSQL.noop,
        ),
        # Step 2: nullify rows whose branch no longer exists
        migrations.RunSQL(
            sql="""
                UPDATE branch_testimonialconversation
                SET branch_id = NULL
                WHERE branch_id IS NOT NULL
                  AND branch_id NOT IN (SELECT id FROM branch_branch);
            """,
            reverse_sql=migrations.RunSQL.noop,
        ),
        # Step 3: update FK constraint (nullable + SET NULL)
        migrations.AlterField(
            model_name='testimonialconversation',
            name='branch',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='testimonials', to='branch.branch', verbose_name='Торговая точка'),
        ),
    ]
