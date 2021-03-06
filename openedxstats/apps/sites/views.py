from __future__ import unicode_literals

import csv
from datetime import datetime, timedelta
import json
import re
from urllib import parse

from django.shortcuts import render, get_object_or_404
from django.views import generic
from django.http import HttpResponseRedirect, HttpResponse
from django.core.urlresolvers import reverse, reverse_lazy
from django.contrib import messages
from django.core import serializers
from django.core.exceptions import ValidationError
from django.db.models import Count, Sum, Q
from django.views.decorators.csrf import csrf_exempt

from openedxstats.apps.sites.models import (
    Site, SiteLanguage, SiteGeoZone, Language, GeoZone, SiteSummarySnapshot,
    AccessLogAggregate, OverCount,
)
from openedxstats.apps.sites.forms import SiteForm, LanguageForm, GeoZoneForm


class ListView(generic.ListView):
    model = Site
    template_name = 'sites/sites_list.html'
    context_object_name = 'sites_list'


class SiteDetailView(generic.DetailView):
    model = Site
    template_name = 'sites/site_detail.html'
    context_object_name = 'site'


class SiteDelete(generic.DeleteView):
    model = Site
    template_name = 'sites/delete_site.html'
    success_url = reverse_lazy('sites:sites_list')


def json_response(text=None, data=None, **response_kwargs):
    """Create a JSON response"""
    if text is None:
        text = json.dumps(data)
    return HttpResponse(text, content_type='application/json', **response_kwargs)


def get_netloc(url):
    """
    Return domain of url if parseable
    """
    if '//' in url:
        netloc = parse.urlparse(url).netloc
    else:
        netloc = url
    return netloc.rstrip(".")


class SiteDiscoveryListView(generic.TemplateView):
    template_name = 'sites/site_discovery.html'

    def discover_domains(self, start_date, end_date):
        """
        Grab aggregate referrer logs from database (that were generated by referrer log script), and compare to sites
        on record, returning domain names that are not in sites list.
        """

        # This is hundreds of thousands of records, and will grow considerably unless the database is regularly cleaned
        # If loading the discovery page begins to lag considerably, a default query other than all data available
        # (e.g. logs from the last day only) should be considered to shorten initial load time
        sites = Site.objects.all()
        known_domains = set()
        known_domains.update(get_netloc(site.url) for site in sites)
        known_domains.update(get_netloc(url) for site in sites for url in site.aliases)

        # Filter out all logs with domains that are: aws owned, edx owned, blank, an ip address, and/or have a custom port
        # Then aggregate based on domain and access date
        aggregate_logs_by_day = AccessLogAggregate.objects.filter(
            (~Q(domain__endswith='.amazonaws.com') & ~Q(domain__endswith='.edx.org') & ~Q(domain='') &
             ~Q(domain__regex=r'^[0-9]+(?:\.[0-9]+){3}$') & ~Q(domain__regex=r':[0-9]+'))
        ).values(
            'access_date',
            'domain'
        ).annotate(
            count=Sum('access_count')
        )

        # If date range specified, filter dates accordingly
        if start_date != '' and end_date != '':
            aggregate_logs_by_day = aggregate_logs_by_day.filter(access_date__range=[start_date, end_date])

        # Filter out objects that don't fall within date range and then combine into one record
        aggregate_logs_by_day = aggregate_logs_by_day.values('domain').annotate(count=Sum('access_count'))

        # Add to final query set if domain isn't already on record
        chaff = ['www', 'studio', 'staging', 'preview', 'stage', 'cms']
        new_domains = []
        for log in aggregate_logs_by_day:
            netloc = get_netloc(log['domain'])
            short_netloc = ".".join(part for part in netloc.split(".") if part not in chaff)
            if netloc not in known_domains and short_netloc not in known_domains:
                new_domains.append(log)

        return new_domains

    def post(self, request, *args, **kwargs):
        new_domains = self.discover_domains(request.POST['start_date'], request.POST['end_date'])
        return json_response(data=new_domains)

    def render_to_response(self, context):
        return super(SiteDiscoveryListView, self).render_to_response(context)


class OTChartView(generic.list.MultipleObjectTemplateResponseMixin, generic.list.BaseListView):
    model = SiteSummarySnapshot
    template_name = 'sites/ot_chart.html'
    context_object_name = 'snapshot_list'

    def daterange(self, start_date, end_date):
        """
        This function is used to generate a date range, with one day increments. Notice the +1 adjustment on day, and -1
        adjustment on seconds. This is used to ensure each day is actually a datetime of the last second of that day, to
        allow for correct aggregation when querying the database with these dates.
        """
        for n in range(int((end_date - start_date).days)):
            yield start_date + timedelta(days=n+1, seconds=-1)

    def generate_summary_data(self, start_datetime):
        """
        Generate site total and course totals by day since ending of site summary snapshots were recorded
        """
        daily_summary_obj_list = []
        # Generate a summary of total sites and courses active for each day in a range of dates
        for day in self.daterange(start_datetime, datetime.now() + timedelta(days=1)):
            # Query to get all site versions that are active within the specified date period
            # We only count public sites with > 0 courses, and count all private sites
            date_select = (
                Q(active_start_date__lte=day) &
                (Q(active_end_date__gte=day) | Q(active_end_date=None))
            )
            day_stats = Site.objects.filter(
                (Q(course_count__gt=0) | Q(is_private_instance=True)) &
                Q(is_gone=False) &
                date_select
            ).aggregate(sites=Count('*'), courses=Sum('course_count'))

            try:
                over_count = OverCount.objects.get(date_select).course_count
            except OverCount.DoesNotExist:
                over_count = 0

            # Generate summary object for day
            daily_summary_obj = SiteSummarySnapshot(
                timestamp=day,
                num_sites=day_stats['sites'],
                num_courses=day_stats['courses'] - over_count,
                notes="Auto-generated day summary"
            )
            daily_summary_obj_list.append(daily_summary_obj)

        return daily_summary_obj_list

    def post(self, request, *args, **kwargs):
        old_ot_data = []
        new_ot_data = []
        if SiteSummarySnapshot.objects.count() > 0 or Site.objects.count() > 0:
            # Get old data (pre-historical tracking implementation)
            old_ot_data = list(SiteSummarySnapshot.objects.all())
            # Gets oldest site summary snapshot from db, after this point we will generate statistics from site versions
            start_datetime = SiteSummarySnapshot.objects.all().order_by('-timestamp').first().timestamp + timedelta(days=1)
            # Generate new data
            new_ot_data = self.generate_summary_data(start_datetime)

        serialized_data = serializers.serialize('json', old_ot_data+new_ot_data)
        return json_response(text=serialized_data)

    def render_to_response(self, context):
        return super(OTChartView, self).render_to_response(context)


def add_site(request, pk=None):
    if pk:
        s = get_object_or_404(Site, pk=pk)
        # Do not allow edits of old versions
        # TODO: Make this more user friendly
        if s.active_end_date is not None:
            response = HttpResponse()
            response.status_code = 403
            return response
    else:
        s = Site()

    if request.method == 'POST':
        form = SiteForm(request.POST, instance=s)
        if form.is_valid():
            new_site = form.save(commit=False)
            new_form_created_time = new_site.active_start_date
            # We must check for uniqueness explicitly, as SiteForm has trouble raising unique key errors for duplicate
            # site entries when trying to update a site
            try:
                new_site.pk = None
                new_site.validate_unique()
            except ValidationError as err:
                messages.error(request, ",".join(err.messages))
                return render_site_form(request, form, pk)

            if pk: # If updating
                # We must grab the same object from the DB again, as s is linked to the form and using it here will
                # cause duplicate key errors
                old_version = Site.objects.get(pk=pk)
                old_version.active_end_date = new_form_created_time
                old_version.save()
            else:
                # Check if there are other versions of site
                if Site.objects.filter(url=new_site.url).count() > 0:
                    next_most_recent_version_of_site = None
                    # Check if any existing sites were created with a more recent start date
                    for site in Site.objects.filter(url=new_site.url).order_by('active_start_date'):
                        if site.active_start_date > new_form_created_time:
                            next_most_recent_version_of_site = site
                            break

                    if next_most_recent_version_of_site is not None:
                        # The version being submitted is older than current version
                        new_site.active_end_date = next_most_recent_version_of_site.active_start_date
                    else:
                        # The version being submitted is newer than current version
                        next_most_recent_version_of_site = Site.objects.filter(url=new_site.url).order_by(
                            '-active_start_date').first()
                        next_most_recent_version_of_site.active_end_date = new_form_created_time
                        next_most_recent_version_of_site.save()

            languages = form.cleaned_data.pop('language')
            geozones = form.cleaned_data.pop('geography')

            new_site.save(force_insert=True)

            if pk: # Delete existing languages and geographies if updating to prevent duplicates
                new_site.language.clear()
                new_site.geography.clear()

            for l in languages:
                site_language = SiteLanguage.objects.create(language=l, site=new_site)
                site_language.save()

            for g in geozones:
                site_geozone = SiteGeoZone.objects.create(geo_zone=g, site=new_site)
                site_geozone.save()

            messages.success(request, 'Success! A new site version has been added!')
            return HttpResponseRedirect(reverse('sites:sites_list'))

        else:
            # Display errors
            form_errors_string = generate_form_errors_string(form.errors)
            messages.error(request, 'Oops! Something went wrong! Details: %s' % form_errors_string)

    else:
        if pk:
            form = SiteForm(initial={'active_start_date':datetime.now()}, instance=s)
        else:
            form = SiteForm()

    return render_site_form(request, form, pk)


def add_language(request):
    l = Language()

    if request.method == 'POST':
        form = LanguageForm(request.POST, instance=l)
        if form.is_valid():
            form.save()
            messages.success(request, 'Success! A new language has been added!')
            return HttpResponseRedirect(reverse('sites:sites_list'))
        else:
            # Display errors
            form_errors_string = generate_form_errors_string(form.errors)
            messages.error(request, 'Oops! Something went wrong! Details: %s' % form_errors_string)
    else:
        form = LanguageForm()

    return render(request, 'add_language.html', {'form':form})


def add_geozone(request):
    g = GeoZone()

    if request.method == 'POST':
        form = GeoZoneForm(request.POST, instance=g)
        if form.is_valid():
            form.save()
            messages.success(request, 'Success! A new geozone has been added!')
            return HttpResponseRedirect(reverse('sites:sites_list'))
        else:
            # Display errors
            form_errors_string = generate_form_errors_string(form.errors)
            messages.error(request, 'Oops! Something went wrong! Details: %s' % form_errors_string)
    else:
        form = GeoZoneForm()

    return render(request, 'add_geozone.html', {'form': form})


def generate_form_errors_string(form_errors):
    """
    Helper function for generating form errors
    """
    form_errors_string = ""
    for i, err in enumerate(form_errors):
        err_description = re.search(r'<li>(.*?)</li>', str(form_errors[err]), re.I).group(1)
        form_errors_string += err + ": " + err_description + ", "
        if i == len(form_errors) - 1:
            form_errors_string = form_errors_string[:-2]

    return form_errors_string

def render_site_form(request, form, pk):
    """
    Helper function for add_site
    """
    if pk:
        return render(request, 'add_site.html',
                      {'form': form, 'post_url': reverse('sites:update_site', args=[pk]), 'page_title': 'Update Site'})
    else:
        return render(request, 'add_site.html',
                      {'form': form, 'post_url': reverse('sites:add_site'), 'page_title': 'Add Site'})

def sites_csv_view(request):
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="sites.csv"'

    complete = bool(request.GET.get('complete', ''))

    sites = Site.objects.filter(active_end_date=None)
    if not complete:
        sites = [site for site in sites if not site.is_gone]

    attrs = ['name', 'url', 'course_count']
    if complete:
        attrs.extend(['is_private_instance', 'is_gone'])
    methods = ['languages', 'geographies']
    other = ['updated']
    writer = csv.DictWriter(response, fieldnames=attrs + methods + other)
    writer.writeheader()
    for site in sites:
        row = {a: getattr(site, a) for a in attrs}
        row.update({m: getattr(site, 'get_'+m)() for m in methods})
        row.update({'updated': site.active_start_date.replace(microsecond=0)})
        writer.writerow(row)

    return response


@csrf_exempt
def bulk_update(request):
    updates = json.loads(request.body.decode('utf8'))

    now = datetime.now()
    updated = []
    not_found = []

    for siteurl, update in updates['sites'].items():
        site_match = Q(url__endswith=siteurl) | Q(url__endswith=siteurl+"/")
        sites = Site.objects.filter(site_match, active_end_date=None)
        if not sites:
            not_found.append(siteurl)
            continue
        site = sites[0]

        # The old Site ends now.
        site.active_end_date = now
        site.save()

        languages = list(site.language.all())
        geo_zones = list(site.geography.all())

        # Make a copy of the site with new info.
        site.pk = None
        site.active_start_date = now
        site.active_end_date = None
        site.course_count = update['course_count']
        site.is_gone = update['is_gone']
        site.save()

        for lang in languages:
            SiteLanguage.objects.create(language=lang, site=site).save()
        for geo_zone in geo_zones:
            SiteGeoZone.objects.create(geo_zone=geo_zone, site=site).save()

        updated.append(siteurl)

    resp = {'updated': updated, 'not_found': not_found}

    over_count = updates.get("overcount")
    if over_count is not None:
        OverCount.set_latest(over_count)
        resp['updated_over_count'] = True
    else:
        resp['updated_over_count'] = False

    return json_response(data=resp)
