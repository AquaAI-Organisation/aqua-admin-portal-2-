from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("admin_portal", "0015_externaloutstandingtoken"),
    ]

    operations = [
        migrations.CreateModel(
            name="AutomationLease",
            fields=[
                ("id", models.PositiveSmallIntegerField(default=1, primary_key=True, serialize=False)),
                ("locked_until", models.DateTimeField(blank=True, null=True)),
                ("holder", models.CharField(blank=True, default="", max_length=120)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
        ),
    ]
