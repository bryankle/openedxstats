# -*- coding: utf-8 -*-
# Generated by Django 1.9.5 on 2018-02-05 18:28
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('sites', '0010_overcount'),
    ]

    operations = [
        migrations.AlterField(
            model_name='overcount',
            name='course_count',
            field=models.IntegerField(default=0),
            preserve_default=False,
        ),
    ]
