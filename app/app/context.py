# -*- coding: utf-8 -*-
"""Define additional context data to be passed to any request.

Copyright (C) 2020 Gitcoin Core

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU Affero General Public License as published
by the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
GNU Affero General Public License for more details.

You should have received a copy of the GNU Affero General Public License
along with this program. If not, see <http://www.gnu.org/licenses/>.

"""
import json
import logging

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

import requests
from app.utils import get_location_from_ip
from cacheops import cached_as
from chat.tasks import get_chat_url
from dashboard.models import Activity, Tip, UserAction
from dashboard.utils import _get_utm_from_cookie
from kudos.models import KudosTransfer
from marketing.utils import handle_marketing_callback
from perftools.models import JSONStore
from retail.helpers import get_ip
from townsquare.models import Announcement

RECORD_VISIT_EVERY_N_SECONDS = 60 * 60

logger = logging.getLogger(__name__)


@cached_as(JSONStore.objects.filter(view='posts', key='posts'), timeout=1200)
def fetchPost(qt='2'):
    jsonstores = JSONStore.objects.filter(view='posts', key='posts')
    if jsonstores.exists():
        return jsonstores.first().data


@cached_as(Announcement.objects.filter(key__in=['footer', 'header']), timeout=1200)
def get_sitewide_announcements():
    announcements = Announcement.objects.filter(
        key__in=['footer', 'header'], valid_to__gt=timezone.now(), valid_from__lt=timezone.now()
    )
    announcement = announcements.filter(key='footer').first()
    header_msg, footer_msg, nav_salt = '', '', 0
    if announcement:
        footer_msg = announcement.title + announcement.desc
    announcement = announcements.filter(key='header').first()
    if announcement:
        header_msg = announcement.title + announcement.desc
        nav_salt = announcement.rank
    return header_msg, footer_msg, nav_salt


def preprocess(request):
    """Handle inserting pertinent data into the current context."""

    # make lbcheck super lightweight
    if request.path == '/lbcheck':
        return {}

    chat_url = get_chat_url(front_end=True)
    chat_access_token = ''
    chat_id = ''

    user_is_authenticated = request.user.is_authenticated
    profile = request.user.profile if user_is_authenticated and hasattr(request.user, 'profile') else None
    if user_is_authenticated and profile and profile.pk:
        # what actions to take?
        record_join = not profile.last_visit
        record_visit = not profile.last_visit or profile.last_visit < (
            timezone.now() - timezone.timedelta(seconds=RECORD_VISIT_EVERY_N_SECONDS)
        )
        if record_visit:
            try:
                profile.last_visit = timezone.now()
                profile.save()
            except Exception as e:
                logger.exception(e)
            try:
                from dashboard.tasks import profile_dict
                profile_dict.delay(profile.pk)
            except Exception as e:
                logger.exception(e)
            metadata = {
                'useragent': request.META['HTTP_USER_AGENT'],
                'referrer': request.META.get('HTTP_REFERER', None),
                'path': request.META.get('PATH_INFO', None),
            }
            ip_address = get_ip(request)
            UserAction.objects.create(
                user=request.user,
                profile=profile,
                action='Visit',
                location_data=get_location_from_ip(ip_address),
                ip_address=ip_address,
                utm=_get_utm_from_cookie(request),
                metadata=metadata,
            )

        if record_join:
            Activity.objects.create(profile=profile, activity_type='joined')

        chat_access_token = profile.gitcoin_chat_access_token
        chat_id = profile.chat_id
    # handles marketing callbacks
    if request.GET.get('cb'):
        callback = request.GET.get('cb')
        handle_marketing_callback(callback, request)

    header_msg, footer_msg, nav_salt = get_sitewide_announcements()

    context = {
        'STATIC_URL': settings.STATIC_URL,
        'MEDIA_URL': settings.MEDIA_URL,
        'chat_url': chat_url,
        'chat_id': chat_id,
        'chat_access_token': chat_access_token,
        'github_handle': request.user.username.lower() if user_is_authenticated else False,
        'email': request.user.email if user_is_authenticated else False,
        'name': request.user.get_full_name() if user_is_authenticated else False,
        'last_chat_status':
            request.user.profile.last_chat_status if
            (hasattr(request.user, 'profile') and user_is_authenticated) else False,
        'raven_js_version': settings.RAVEN_JS_VERSION,
        'raven_js_dsn': settings.SENTRY_JS_DSN,
        'release': settings.RELEASE,
        'env': settings.ENV,
        'header_msg': header_msg,
        'nav_salt': nav_salt,
        'footer_msg': footer_msg,
        'INFURA_V3_PROJECT_ID': settings.INFURA_V3_PROJECT_ID,
        'giphy_key': settings.GIPHY_KEY,
        'youtube_key': settings.YOUTUBE_API_KEY,
        'orgs': profile.organizations if profile else [],
        'profile_id': profile.id if profile else '',
        'hotjar': settings.HOTJAR_CONFIG,
        'ipfs_config': {
            'host': settings.JS_IPFS_HOST,
            'port': settings.IPFS_API_PORT,
            'protocol': settings.IPFS_API_SCHEME,
            'root': settings.IPFS_API_ROOT,
        },
        'chat_persistence_frequency': 90 * 1000,
        'access_token': profile.access_token if profile else '',
        'is_staff': request.user.is_staff if user_is_authenticated else False,
        'is_moderator': profile.is_moderator if profile else False,
        'is_alpha_tester': profile.is_alpha_tester if profile else False,
        'persona_is_funder': profile.persona_is_funder if profile else False,
        'persona_is_hunter': profile.persona_is_hunter if profile else False,
        'profile_url': profile.url if profile else False,
        'quests_live': settings.QUESTS_LIVE,
    }
    context['json_context'] = json.dumps(context)
    context['last_posts'] = cache.get_or_set('last_posts', fetchPost, 5000)

    if context['github_handle']:
        context['unclaimed_tips'] = Tip.objects.filter(
            receive_txid='', username__iexact=context['github_handle'], web3_type='v3',
        ).send_happy_path().cache(timeout=60)
        context['unclaimed_kudos'] = KudosTransfer.objects.filter(
            receive_txid='', username__iexact="@" + context['github_handle'], web3_type='v3',
        ).send_happy_path().cache(timeout=60)

        if not settings.DEBUG:
            context['unclaimed_tips'] = context['unclaimed_tips'].filter(network='mainnet').cache(timeout=60)
            context['unclaimed_kudos'] = context['unclaimed_kudos'].filter(network='mainnet').cache(timeout=60)

    return context
