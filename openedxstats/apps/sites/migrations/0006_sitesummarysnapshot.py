# -*- coding: utf-8 -*-
# Generated by Django 1.9.5 on 2016-07-01 18:27
from __future__ import unicode_literals

import datetime
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('sites', '0005_auto_20160627_1743'),
    ]

    operations = [
        migrations.CreateModel(
            name='SiteSummarySnapshot',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('timestamp', models.DateTimeField(default=datetime.datetime.now)),
                ('num_sites', models.IntegerField()),
                ('num_courses', models.IntegerField()),
                ('notes', models.TextField(blank=True)),
            ],
        ),
    ]
