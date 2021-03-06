from __future__ import unicode_literals

import base64
import hashlib
import hmac
import json
from django.core.exceptions import ValidationError
import phonenumbers
import pytz
import time
from collections import OrderedDict

from datetime import datetime, timedelta
from django.db.models import Count
from django.http import Http404
from django.shortcuts import redirect
from django.utils import timezone
from django.utils.translation import ugettext_lazy as _
from django_countries.data import COUNTRIES
from phonenumbers.phonenumberutil import region_code_for_number
from smartmin.views import *
from temba.contacts.models import TEL_SCHEME, TWITTER_SCHEME, EMAIL_SCHEME
from temba.orgs.models import Org
from temba.orgs.views import OrgPermsMixin, OrgObjPermsMixin, ModalMixin
from temba.msgs.models import Msg, Broadcast, Call, QUEUED, PENDING
from temba.utils.middleware import disable_middleware
from temba.utils import analytics, non_atomic_when_eager
from twilio import TwilioRestException
from twython import Twython
from uuid import uuid4
from .models import Channel, SyncEvent, Alert, ChannelLog, SHAQODOON
from .models import PASSWORD, RECEIVE, SEND, SEND_METHOD, SEND_URL, USERNAME
from .models import ANDROID, EXTERNAL, HUB9, INFOBIP, KANNEL, NEXMO, TWILIO, TWITTER, VUMI
from .models import Channel, SyncEvent, Alert, ChannelLog
from .models import PASSWORD, RECEIVE, SEND, CALL, ANSWER, SEND_METHOD, SEND_URL, USERNAME
from .models import ANDROID, EXTERNAL, HUB9, INFOBIP, KANNEL, NEXMO, TWILIO, TWITTER, VUMI, VERBOICE, SHAQODOON

RELAYER_TYPE_ICONS = {ANDROID: "icon-channel-android",
                      EXTERNAL: "icon-channel-external",
                      KANNEL: "icon-channel-kannel",
                      NEXMO: "icon-channel-nexmo",
                      VERBOICE: "icon-channel-external",
                      TWILIO: "icon-channel-twilio",
                      TWITTER: "icon-twitter"}

SESSION_TWITTER_TOKEN = 'twitter_oauth_token'
SESSION_TWITTER_SECRET = 'twitter_oauth_token_secret'

TWILIO_SEARCH_COUNTRIES = (('BE', _("Belgium")),
                           ('CA', _("Canada")),
                           ('FI', _("Finland")),
                           ('NO', _("Norway")),
                           ('PL', _("Poland")),
                           ('ES', _("Spain")),
                           ('SE', _("Sweden")),
                           ('GB', _("United Kingdom")),
                           ('US', _("United States")))

TWILIO_SUPPORTED_COUNTRIES = (('AU', _("Australia")),
                              ('AT', _("Austria")),
                              ('BE', _("Belgium")),
                              ('CA', _("Canada")),
                              ('EE', _("Estonia")),
                              ('FI', _("Finland")),
                              ('HK', _("Hong Kong")),
                              ('IE', _("Ireland")),
                              ('LT', _("Lithuania")),
                              ('NO', _("Norway")),
                              ('PL', _("Poland")),
                              ('ES', _("Spain")),
                              ('SE', _("Sweden")),
                              ('CH', _("Switzerland")),
                              ('GB', _("United Kingdom")),
                              ('US', _("United States")))

TWILIO_SUPPORTED_COUNTRY_CODES = [61, 43, 32, 1, 372, 358, 852, 353, 370, 47, 48, 34, 46, 41, 44]

NEXMO_SUPPORTED_COUNTRIES = (('AU', _('Australia')),
                             ('AT', _('Austria')),
                             ('BE', _('Belgium')),
                             ('CA', _('Canada')),
                             ('CL', _('Chile')),
                             ('CR', _('Costa Rica')),
                             ('CZ', _('Czech Republic')),
                             ('DK', _('Denmark')),
                             ('EE', _('Estonia')),
                             ('FI', _('Finland')),
                             ('FR', _('France')),
                             ('DE', _('Germany')),
                             ('HK', _('Hong Kong')),
                             ('HU', _('Hungary')),
                             ('ID', _('Indonesia')),
                             ('IE', _('Ireland')),
                             ('IL', _('Israel')),
                             ('IT', _('Italy')),
                             ('LV', _('Latvia')),
                             ('LT', _('Lithuania')),
                             ('MY', _('Malaysia')),
                             ('MX', _('Mexico')),
                             ('MW', _('Malawi')),
                             ('NL', _('Netherlands')),
                             ('NO', _('Norway')),
                             ('PK', _('Pakistan')),
                             ('PL', _('Poland')),
                             ('PR', _('Puerto Rico')),
                             ('RO', _('Romania')),
                             ('RU', _('Russia')),
                             ('RW', _('Rwanda')),
                             ('SK', _('Slovakia')),
                             ('ZA', _('South Africa')),
                             ('KR', _('South Korea')),
                             ('ES', _('Spain')),
                             ('SE', _('Sweden')),
                             ('CH', _('Switzerland')),
                             ('GB', _('United Kingdom')),
                             ('US', _('United States')))

NEXMO_SUPPORTED_COUNTRY_CODES  = [61, 43, 32, 1, 56, 506, 420, 45, 372, 358, 33, 49, 852, 36, 353, 972, 39, 371, 370,
                                  60, 52, 31, 47, 92, 48, 1787, 40, 7, 250, 421, 27, 82, 34, 46, 41, 44, 265]


# django_countries now uses a dict of countries, let's turn it in our tuple
# list of codes and countries sorted by country name
ALL_COUNTRIES = sorted(((code, name) for code, name in COUNTRIES.items()), key=lambda x: x[1])

def get_channel_icon(channel_type):
    return RELAYER_TYPE_ICONS.get(channel_type, "icon-channel-external")


def channel_status_processor(request):
    status = dict()
    user = request.user

    if user.is_superuser or user.is_anonymous():
        return status

    # from the logged in user get the channel
    org = user.get_org()

    allowed = False
    if org:
        # user must be admin or editor
        allowed = org.get_org_admins().filter(pk=user.pk) | org.get_org_editors().filter(pk=user.pk)

    if allowed:

        # only care about channels that are older than an hour
        cutoff = timezone.now() - timedelta(hours=1)
        send_channel = org.get_send_channel(scheme=TEL_SCHEME)
        call_channel = org.get_call_channel()

        # twitter is a suitable sender
        if not send_channel:
            send_channel = org.get_send_channel(scheme=TWITTER_SCHEME)

        status['send_channel'] = send_channel
        status['call_channel'] = call_channel
        status['has_outgoing_channel'] = send_channel or call_channel

        channels = org.channels.filter(is_active=True)
        for channel in channels:

            if channel.created_on > cutoff:
                continue

            if not channel.is_new():
                # delayed out going messages
                if channel.get_delayed_outgoing_messages():
                    status['unsent_msgs'] = True

                # see if it hasn't synced in a while
                if not channel.get_recent_syncs():
                    status['delayed_syncevents'] = True

                # don't have to keep looking if they've both failed
                if 'delayed_syncevents' in status and 'unsent_msgs' in status:
                    break

    return status


def get_commands(channel, commands, sync_event=None):

    # we want to find all queued messages

    # all outgoing messages for our channel that are queued up
    broadcasts = Broadcast.objects.filter(status__in=[QUEUED, PENDING], schedule=None,
                                          msgs__channel=channel).distinct().order_by('created_on', 'pk')

    outgoing_messages = 0
    for broadcast in broadcasts:
        # Send command looks like this:
        # {
        #    "cmd":"send",
        #    "to":[{number:"250788382384", "id":26],
        #    "msg":"Is water point A19 still functioning?"
        # }
        msgs = broadcast.get_messages().filter(status__in=[PENDING, QUEUED]).exclude(topup=None)

        if sync_event:
            pending_msgs = sync_event.get_pending_messages()
            retry_msgs = sync_event.get_retry_messages()
            msgs = msgs.exclude(pk__in=pending_msgs).exclude(pk__in=retry_msgs)

        outgoing_messages += len(msgs)

        if msgs:
            commands += broadcast.get_sync_commands(channel=channel)

    # TODO: add in other commands for the channel
    # We need a queueable model similar to messages for sending arbitrary commands to the client

    return commands

@disable_middleware
def sync(request, channel_id):
    start = time.time()

    if request.method != 'POST':
        return HttpResponse(status=500, content='POST Required')

    commands = []
    channel = Channel.objects.filter(pk=channel_id, is_active=True)
    if not channel:
        return HttpResponse(json.dumps(dict(cmds=[dict(cmd='rel', relayer_id=channel_id)])), content_type='application/javascript')

    channel = channel[0]

    request_time = request.REQUEST.get('ts', '')
    request_signature = request.REQUEST.get('signature', '')

    if not channel.secret or not channel.org:
        return HttpResponse(json.dumps(dict(cmds=[channel.build_registration_command()])), content_type='application/javascript')

    #print "\n\nSECRET: '%s'" % channel.secret
    #print "TS: %s" % request_time
    #print "BODY: '%s'\n\n" % request.body

    # check that the request isn't too old (15 mins)
    now = time.time()
    if abs(now - int(request_time)) > 60 * 15:
        return HttpResponse(status=401, content='{ "error_id": 3, "error": "Old Request", "cmds":[] }')

    # sign the request
    signature = hmac.new(key=str(channel.secret + request_time), msg=bytes(request.body), digestmod=hashlib.sha256).digest()

    # base64 and url sanitize
    signature = base64.urlsafe_b64encode(signature).strip()

    if request_signature != signature:
        return HttpResponse(status=401,
                            content='{ "error_id": 1, "error": "Invalid signature: \'%(request)s\'", "cmds":[] }' % {'request':request_signature})

    # update our last seen on our channel
    channel.last_seen = timezone.now()
    channel.save()

    sync_event = None

    # Take the update from the client
    if request.body:

        client_updates = json.loads(request.body)

        print "==GOT SYNC"
        print json.dumps(client_updates, indent=2)

        incoming_count = 0
        if 'cmds' in client_updates:
            cmds = client_updates['cmds']

            for cmd in cmds:
                handled = False
                extra = None

                if 'cmd' in cmd:
                    keyword = cmd['cmd']

                    # catchall for commands that deal with a single message
                    if 'msg_id' in cmd:
                        msg = Msg.objects.filter(pk=cmd['msg_id'],
                                                 org=channel.org)
                        if msg:
                            msg = msg[0]
                            handled = msg.update(cmd)

                    # creating a new message
                    elif keyword == 'mo_sms':
                        date = datetime.fromtimestamp(int(cmd['ts']) / 1000).replace(tzinfo=pytz.utc)

                        msg = Msg.create_incoming(channel, (TEL_SCHEME, cmd['phone']), cmd['msg'], date=date)
                        if msg:
                            extra = dict(msg_id=msg.id)
                            handled = True

                    # phone event
                    elif keyword == 'call':
                        date = datetime.fromtimestamp(int(cmd['ts']) / 1000).replace(tzinfo=pytz.utc)

                        duration = 0
                        if cmd['type'] != 'miss':
                            duration = cmd['dur']

                        # Android sometimes will pass us a call from an 'unknown number', which is null
                        # ignore these events on our side as they have no purpose and break a lot of our
                        # assumptions
                        if cmd['phone']:
                            Call.create_call(channel=channel,
                                             phone=cmd['phone'],
                                             date=date,
                                             duration=duration,
                                             call_type=cmd['type'])
                        handled = True

                    elif keyword == 'gcm':
                        # update our gcm and uuid
                        channel.gcm_id = cmd['gcm_id']
                        channel.uuid = cmd.get('uuid', None)
                        channel.save()

                        # no acking the gcm
                        handled = False

                    elif keyword == 'reset':
                        # release this channel
                        channel.release(False)
                        channel.save()

                        # ack that things got handled
                        handled = True

                    elif keyword == 'status':
                        sync_event = SyncEvent.create(channel, cmd, cmds)
                        Alert.check_power_alert(sync_event)

                        # tell the channel to update its org if this channel got moved
                        if channel.org and 'org_id' in cmd and channel.org.pk != cmd['org_id']:
                            commands.append(dict(cmd='claim', org_id=channel.org.pk))

                        # we don't ack status messages since they are always included
                        handled = False

                # is this something we can ack?
                if 'p_id' in cmd and handled:
                    ack = dict(p_id=cmd['p_id'], cmd="ack")
                    if extra:
                        ack['extra'] = extra

                    commands.append(ack)

    outgoing_cmds = get_commands(channel, commands, sync_event)
    result = dict(cmds=outgoing_cmds)

    if sync_event:
        sync_event.outgoing_command_count = len([_ for _ in outgoing_cmds if _['cmd'] != 'ack'])
        sync_event.save()

    print "==RESPONDING WITH:"
    print json.dumps(result, indent=2)

    # keep track of how long a sync takes
    analytics.track(channel.created_by.username, "temba.relayer_sync", properties=dict(value=time.time() - start))

    return HttpResponse(json.dumps(result), content_type='application/javascript')

@disable_middleware
def register(request):
    if request.method != 'POST':
        return HttpResponse(status=500, content=_('POST Required'))

    client_payload = json.loads(request.body)
    cmds = client_payload['cmds']

    # look up a channel with that id
    channel = Channel.from_gcm_and_status_cmds(cmds[0], cmds[1])
    cmd = channel.build_registration_command()

    result = dict(cmds=[cmd])
    return HttpResponse(json.dumps(result), content_type='application/javascript')

class ClaimForm(forms.Form):
    claim_code = forms.CharField(max_length=12,
                                 help_text=_("The claim code from your Android phone"))
    phone_number = forms.CharField(max_length=15,
                                   help_text=_("The phone number of the phone"))

    def clean_claim_code(self):
        claim_code = self.cleaned_data['claim_code']
        claim_code = claim_code.replace(' ', '').upper()

        # is there a channel with that claim?
        channel = Channel.objects.filter(claim_code=claim_code, is_active=True)

        if not channel:
            raise forms.ValidationError(_("Invalid claim code, please check and try again."))
        else:
            self.cleaned_data['channel'] = channel[0]

        return claim_code

    def clean_phone_number(self):
        number = self.cleaned_data['phone_number']

        if 'channel' in self.cleaned_data:
            try:
                normalized = phonenumbers.parse(number, self.cleaned_data['channel'].country.code)
                if not phonenumbers.is_possible_number(normalized):
                    raise forms.ValidationError(_("Invalid phone number, try again."))
            except: # pragma: no cover
                raise forms.ValidationError(_("Invalid phone number, try again."))

            number = phonenumbers.format_number(normalized, phonenumbers.PhoneNumberFormat.E164)

        return number


class UpdateChannelForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        self.object = kwargs['object']
        del kwargs['object']

        super(UpdateChannelForm, self).__init__(*args, **kwargs)
        self.add_config_fields()

    def add_config_fields(self):
        pass

    class Meta:
        model = Channel
        fields = 'name', 'address', 'country', 'alert_email'
        config_fields = []
        readonly = 'address', 'country'
        labels = {'address': _('Number')}
        helps = {'address': _('Phone number of this service')}


class UpdateAndroidForm(UpdateChannelForm):
    class Meta(UpdateChannelForm.Meta):
        readonly = []
        helps = {'address': _('Phone number of this device')}


class UpdateTwitterForm(UpdateChannelForm):
    def add_config_fields(self):
        config = json.loads(self.object.config)
        ctrl = forms.BooleanField(label=_("Auto Follow"), initial=config.get('auto_follow', True), required=False,
                                  help_text=_("Automatically follow any account that follows your account"))

        self.fields = OrderedDict(self.fields.items() + [('auto_follow', ctrl)])

    class Meta(UpdateChannelForm.Meta):
        fields = 'name', 'address', 'alert_email'
        config_fields = ('auto_follow',)
        readonly = ('address',)
        labels = {'address': _('Handle')}
        helps = {'address': _('Twitter handle of this channel')}


class ChannelCRUDL(SmartCRUDL):
    model = Channel
    actions = ('list', 'claim', 'update', 'read', 'delete', 'search_numbers', 'claim_number',
               'claim_android', 'claim_africas_talking', 'claim_zenvia', 'configuration', 'claim_external',
               'search_nexmo', 'claim_nexmo', 'bulk_sender_options', 'create_bulk_sender', 'claim_infobip',
               'claim_hub9', 'claim_vumi', 'create_caller', 'claim_kannel', 'claim_twitter', 'claim_shaqodoon', 'claim_verboice')
    permissions = True

    class AnonMixin(OrgPermsMixin):
        """
        Mixin that makes sure that anonymous orgs cannot add channels (have no permission if anon)
        """
        def has_permission(self, request, *args, **kwargs):
            org = self.derive_org()
            if not org or org.is_anon:
                return False
            else:
                return super(ChannelCRUDL.AnonMixin, self).has_permission(request, *args, **kwargs)

    class Read(OrgObjPermsMixin, SmartReadView):
        exclude = ('id', 'is_active', 'created_by', 'modified_by', 'modified_on', 'gcm_id')

        def get_gear_links(self):
            links = []

            if self.has_org_perm("channels.channel_update"):
                links.append(dict(title=_('Edit'),
                                  style='btn-primary',
                                  href=reverse('channels.channel_update', args=[self.get_object().id])))

                sender = self.get_object().get_sender()
                if sender and sender.is_delegate_sender():
                    links.append(dict(title=_('Disable Bulk Sending'),
                                      style='btn-primary',
                                      href="#",
                                      js_class='remove-sender'))
                elif self.get_object().channel_type == ANDROID:
                    links.append(dict(title=_('Enable Bulk Sending'),
                                      style='btn-primary',
                                      href="%s?channel=%d" % (reverse("channels.channel_bulk_sender_options"), self.get_object().pk)))

                caller = self.get_object().get_caller()
                if caller and caller.is_delegate_caller():
                    links.append(dict(title=_('Disable Voice Calling'),
                                      style='btn-primary',
                                      href="#",
                                      js_class='remove-caller'))
                # TODO: Can't posterize from menu, so this shortcut won't work yet
                # elif self.org.is_connected_to_twilio() and self.request.user.is_beta():
                #    links.append(dict(title=_('Enable Voice Calling'),
                #                      style='posterize',
                #                      href="%s?channel=%d" % (reverse("channels.channel_create_caller"), self.get_object().pk)))
            if self.has_org_perm("channels.channel_delete"):
                links.append(dict(title=_('Remove'),
                                  js_class='remove-channel',
                                  href="#"))
            return links

        def get_context_data(self, **kwargs):
            context = super(ChannelCRUDL.Read, self).get_context_data(**kwargs)
            channel = self.object

            sync_events = SyncEvent.objects.filter(channel=channel.id).order_by('-created_on')
            context['last_sync'] = sync_events.first()

            if 'HTTP_X_FORMAX' in self.request.META:  # no additional data needed if request is only for formax
                return context

            if not channel.is_active:
                raise Http404("No active channel with that id")

            context['channel_errors'] = ChannelLog.objects.filter(msg__channel=self.object, is_error=True)
            context['sms_count'] = Msg.objects.filter(channel=self.object).count()

            ## power source stats data
            source_stats = [[_['power_source'], _['count']] for _ in sync_events.order_by('power_source').values('power_source').annotate(count=Count('power_source'))]
            context['source_stats'] = source_stats

            ## network connected to stats
            network_stats = [[_['network_type'], _['count']] for _ in sync_events.order_by('network_type').values('network_type').annotate(count=Count('network_type'))]
            context['network_stats'] = network_stats

            total_network = 0
            network_share = []

            for net in network_stats:
                total_network += net[1]

            total_share = 0
            for net_stat in network_stats:
                share = int(round((100 * net_stat[1]) / float(total_network)))
                net_name = net_stat[0]

                if net_name != "NONE" and net_name != "UNKNOWN" and share > 0:
                    network_share.append([net_name, share])
                    total_share += share

            other_share = 100 - total_share
            if other_share > 0:
                network_share.append(["OTHER", other_share])

            context['network_share'] = sorted(network_share, key=lambda _: _[1], reverse=True)

            # add to context the latest sync events to display in a table
            context['latest_sync_events'] = sync_events[:10]

            # delayed sync event
            if not channel.is_new():
                if sync_events:
                    latest_sync_event = sync_events[0]
                    interval = timezone.now() - latest_sync_event.created_on
                    seconds = interval.seconds + interval.days * 24 * 3600
                    if seconds > 3600:
                        context['delayed_sync_event'] = latest_sync_event

                # unsent messages
                unsent_msgs = channel.get_delayed_outgoing_messages()

                if unsent_msgs:
                    context['unsent_msgs_count'] = unsent_msgs.count()

            end_date = timezone.now()
            start_date = end_date - timedelta(days=30)

            incoming_stats = []
            outgoing_stats = []
            message_stats = []

            # build up the channels we care about for outgoing messages
            outgoing_channels = [channel]
            for sender in Channel.objects.filter(parent=channel):
                outgoing_channels.append(sender)

            # get our incoming messages by date
            incoming = list(channel.msgs.filter(created_on__gte=start_date, direction='I', contact__is_test=False).extra(
                {'date_created': "date(msgs_msg.created_on)"}).order_by('date_created').values('date_created').annotate(created_count=Count('id')))

            outgoing = list(Msg.objects.filter(channel__in=outgoing_channels, created_on__gte=start_date, direction='O', contact__is_test=False).extra(
                {'date_created': "date(msgs_msg.created_on)"}).order_by('date_created').values('date_created').annotate(created_count=Count('id')))

            while start_date <= end_date:
                day = start_date.date()

                in_count = 0
                if incoming and incoming[0]['date_created'] == day:
                    in_count = incoming[0]['created_count']
                    incoming.pop(0)

                out_count = 0
                if outgoing and outgoing[0]['date_created'] == day:
                    out_count = outgoing[0]['created_count']
                    outgoing.pop(0)

                incoming_stats.append(dict(date=start_date, count=in_count))
                outgoing_stats.append(dict(date=start_date, count=out_count))

                start_date += timedelta(days=1)

            message_stats.append(dict(name='Incoming', data=incoming_stats))
            message_stats.append(dict(name='Outgoing', data=outgoing_stats))

            context['message_stats'] = message_stats
            context['has_messages'] = len(incoming_stats) or len(outgoing_stats)

            return context

    class Delete(ModalMixin, OrgObjPermsMixin, SmartDeleteView):
        cancel_url = "id@channels.channel_read"
        title = _("Remove Android")
        success_message = ''
        form = []

        def get_success_url(self):
            return reverse('orgs.org_home')

        def post(self, request, *args, **kwargs):
            channel = self.get_object()

            try:
                if channel.channel_type == TWILIO and not channel.is_delegate_sender():
                    messages.info(request, _("We have disconnected your Twilio number. If you do not need this number you can delete it from the Twilio website."))
                else:
                    messages.info(request, _("Your phone number has been removed."))

                channel.release(trigger_sync=self.request.META['SERVER_NAME'] != "testserver")
                return HttpResponseRedirect(self.get_success_url())

            except Exception as e:
                import traceback
                traceback.print_exc(e)
                messages.error(request, _("We encountered an error removing your channel, please try again later."))
                return HttpResponseRedirect(reverse("channels.channel_read", args=[channel.pk]))

    class Update(OrgObjPermsMixin, SmartUpdateView):
        success_message = ''
        submit_button_name = _("Save Changes")

        def derive_title(self):
            return _("%s Channel") % self.object.get_channel_type_display()

        def derive_readonly(self):
            return self.form.Meta.readonly if hasattr(self, 'form') else []

        def lookup_field_label(self, context, field, default=None):
            if field in self.form.Meta.labels:
                return self.form.Meta.labels[field]
            return super(ChannelCRUDL.Update, self).lookup_field_label(context, field, default=default)

        def lookup_field_help(self, field, default=None):
            if field in self.form.Meta.helps:
                return self.form.Meta.helps[field]
            return super(ChannelCRUDL.Update, self).lookup_field_help(field, default=default)

        def get_success_url(self):
            return reverse('channels.channel_read', args=[self.object.pk])

        def get_form_class(self):
            channel_type = self.object.channel_type
            scheme = self.object.get_scheme()

            if channel_type == ANDROID:
                return UpdateAndroidForm
            elif scheme == TWITTER_SCHEME:
                return UpdateTwitterForm
            else:
                return UpdateChannelForm

        def get_form_kwargs(self):
            kwargs = super(ChannelCRUDL.Update, self).get_form_kwargs()
            kwargs['object'] = self.object
            return kwargs

        def pre_save(self, obj):
            if obj.config:
                config = json.loads(obj.config)
                for field in self.form.Meta.config_fields:
                    config[field] = bool(self.form.cleaned_data[field])
                obj.config = json.dumps(config)
            return obj

        def post_save(self, obj):
            # update our delegate channels with the new number
            if not obj.parent and obj.get_scheme() == TEL_SCHEME:
                e164_phone_number = None
                try:
                    parsed = phonenumbers.parse(obj.address, None)
                    e164_phone_number = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164).strip('+')
                except Exception:
                    pass
                for channel in obj.get_delegate_channels():
                    channel.address = obj.address
                    channel.uuid = e164_phone_number
                    channel.save()
            return obj

    class Claim(OrgPermsMixin, SmartTemplateView):

        def get_context_data(self, **kwargs):
            context = super(ChannelCRUDL.Claim, self).get_context_data(**kwargs)

            twilio_countries = [unicode(c[1]) for c in TWILIO_SEARCH_COUNTRIES]

            twilio_countries_str = ', '.join(twilio_countries[:-1])
            twilio_countries_str += ' ' + unicode(_('or')) + ' ' + twilio_countries[-1]

            context['twilio_countries'] = twilio_countries_str

            return context

    class BulkSenderOptions(OrgPermsMixin, SmartTemplateView):
        pass

    class CreateBulkSender(OrgPermsMixin, SmartFormView):

        class BulkSenderForm(forms.Form):
            connection = forms.CharField(max_length=2, widget=forms.HiddenInput, required=False)

            def __init__(self, *args, **kwargs):
                self.org = kwargs['org']
                del kwargs['org']
                super(ChannelCRUDL.CreateBulkSender.BulkSenderForm, self).__init__(*args, **kwargs)

            def clean_connection(self):
                connection = self.cleaned_data['connection']
                if connection == NEXMO and not self.org.is_connected_to_nexmo():
                    raise forms.ValidationError(_("A connection to a Nexmo account is required"))
                return connection

        form_class = BulkSenderForm
        fields = ('connection', )

        def get_form_kwargs(self, *args, **kwargs):
            form_kwargs = super(ChannelCRUDL.CreateBulkSender, self).get_form_kwargs(*args, **kwargs)
            form_kwargs['org'] = Org.objects.get(pk=self.request.user.get_org().pk)
            return form_kwargs

        def form_valid(self, form):

            # make sure they own the channel
            channel = self.request.REQUEST.get('channel', None)
            if channel:
                channel = self.request.user.get_org().channels.filter(pk=channel).first()
            if not channel:
                raise forms.ValidationError("Can't add sender for that number")

            user = self.request.user

            Channel.add_send_channel(user, channel)
            return super(ChannelCRUDL.CreateBulkSender, self).form_valid(form)

        def form_invalid(self, form):
            return super(ChannelCRUDL.CreateBulkSender, self).form_invalid(form)

        def get_success_url(self):
            return reverse('orgs.org_home')


    class CreateCaller(OrgPermsMixin, SmartFormView):

        class CallerForm(forms.Form):
            connection = forms.CharField(max_length=2, widget=forms.HiddenInput, required=False)

            def __init__(self, *args, **kwargs):
                self.org = kwargs['org']
                del kwargs['org']
                super(ChannelCRUDL.CreateCaller.CallerForm, self).__init__(*args, **kwargs)

            def clean_connection(self):
                connection = self.cleaned_data['connection']
                if connection == TWILIO and not self.org.is_connected_to_twilio():
                    raise forms.ValidationError(_("A connection to a Nexmo account is required"))
                return connection

        form_class = CallerForm
        fields = ('connection', )

        def get_form_kwargs(self, *args, **kwargs):
            form_kwargs = super(ChannelCRUDL.CreateCaller, self).get_form_kwargs(*args, **kwargs)
            form_kwargs['org'] = Org.objects.get(pk=self.request.user.get_org().pk)
            return form_kwargs

        def form_valid(self, form):
            user = self.request.user
            org = user.get_org()

            # make sure they own the channel
            channel = self.request.REQUEST.get('channel', None)
            if channel:
                channel = self.request.user.get_org().channels.filter(pk=channel).first()
            if not channel:
                raise forms.ValidationError(_("Sorry, a caller cannot be added for that number"))

            Channel.add_call_channel(org, user, channel)
            return super(ChannelCRUDL.CreateCaller, self).form_valid(form)

        def form_invalid(self, form):
            return super(ChannelCRUDL.CreateCaller, self).form_invalid(form)

        def get_success_url(self):
            return reverse('orgs.org_home')

    class ClaimZenvia(OrgPermsMixin, SmartFormView):
        class ZVClaimForm(forms.Form):
            shortcode = forms.CharField(max_length=6, min_length=1,
                                        help_text=_("The Zenvia short code"))
            account = forms.CharField(max_length=32,
                                      help_text=_("Your account name on Zenvia"))
            code = forms.CharField(max_length=64,
                                   help_text=_("Your api code on Zenvia for authentication"))

        title = _("Connect Zenvia Account")
        fields = ('shortcode', 'account', 'code')
        form_class = ZVClaimForm
        success_url = "id@channels.channel_configuration"

        def form_valid(self, form):
            org = self.request.user.get_org()

            if not org: # pragma: no cover
                raise Exception(_("No org for this user, cannot claim"))

            data = form.cleaned_data
            self.object = Channel.add_zenvia_channel(org, self.request.user,
                                                     phone=data['shortcode'], account=data['account'], code=data['code'])

            # make sure all contacts added before the channel are normalized
            self.object.ensure_normalized_contacts()

            return super(ChannelCRUDL.ClaimZenvia, self).form_valid(form)

    class ClaimKannel(OrgPermsMixin, SmartFormView):
        class KannelClaimForm(forms.Form):
            number = forms.CharField(max_length=14, min_length=1, label=_("Number"),
                                     help_text=_("The phone number or short code you are connecting"))
            country = forms.ChoiceField(choices=ALL_COUNTRIES, label=_("Country"),
                                        help_text=_("The country this phone number is used in"))
            url = forms.URLField(max_length=1024, label=_("Send URL"),
                                 help_text=_("The publicly accessible URL for your Kannel instance for sending. ex: https://kannel.macklemore.co/cgi-bin/sendsms"))
            username = forms.CharField(max_length=64, required=False,
                                       help_text=_("The username to use to authenticate to Kannel, if left blank we will generate one for you"))
            password = forms.CharField(max_length=64, required=False,
                                       help_text=_("The password to use to authenticate to Kannel, if left blank we will generate one for you"))

        title = _("Connect Kannel Service")
        success_url = "id@channels.channel_configuration"
        form_class = KannelClaimForm

        def form_valid(self, form):
            org = self.request.user.get_org()
            data = form.cleaned_data

            country = data['country']
            url = data['url']
            number = data['number']
            role = SEND + RECEIVE

            config = {SEND_URL: url, USERNAME: data.get('username', None), PASSWORD: data.get('password', None)}
            self.object = Channel.add_config_external_channel(org, self.request.user, country, number, KANNEL,
                                                              config, role=role, parent=None)

            # if they didn't set a username or password, generate them, we do this after the addition above
            # because we use the channel id in the configuration
            config = self.object.config_json()
            if not config.get(USERNAME, None):
                config[USERNAME] = '%s_%d' % (self.request.branding['name'].lower(), self.object.pk)

            if not config.get(PASSWORD, None):
                config[PASSWORD] = str(uuid4())

            self.object.config = json.dumps(config)
            self.object.save()

            # make sure all contacts added before the channel are normalized
            self.object.ensure_normalized_contacts()

            return super(ChannelCRUDL.ClaimKannel, self).form_valid(form)

    class ClaimExternal(OrgPermsMixin, SmartFormView):
        class EXClaimForm(forms.Form):

            number = forms.CharField(max_length=14, min_length=1, label=_("Number"),
                                     help_text=_("The phone number or short code you are connecting"))
            country = forms.ChoiceField(choices=ALL_COUNTRIES, label=_("Country"),
                                        help_text=_("The country this phone number is used in"))
            url = forms.URLField(max_length=1024, label=_("Send URL"),
                                 help_text=_("The URL we will call when sending messages, with variable substitutions"))
            method = forms.ChoiceField(choices=(('POST', "HTTP POST"), ('GET', "HTTP GET"), ('PUT', "HTTP PUT")),
                                       help_text=_("What HTTP method to use when calling the URL"))


        class EXSendClaimForm(forms.Form):
            url = forms.URLField(max_length=1024, label=_("Send URL"),
                                 help_text=_("The URL we will POST to when sending messages, with variable substitutions"))
            method = forms.ChoiceField(choices=(('POST', "HTTP POST"), ('GET', "HTTP GET"), ('PUT', "HTTP PUT")),
                                       help_text=_("What HTTP method to use when calling the URL"))

        title = "Connect External Service"
        success_url = "id@channels.channel_configuration"

        def get_form_class(self):

            if self.request.REQUEST.get('role', None) == 'S':
                return ChannelCRUDL.ClaimExternal.EXSendClaimForm
            else:
                return ChannelCRUDL.ClaimExternal.EXClaimForm

        def form_valid(self, form):
            org = self.request.user.get_org()

            if not org: # pragma: no cover
                raise Exception("No org for this user, cannot claim")

            data = form.cleaned_data

            if self.request.REQUEST.get('role', None) == 'S':
                # get our existing channel
                receive = org.get_receive_channel(TEL_SCHEME)

                country = receive.country
                number = receive.address
                role = SEND

            else:
                country = data['country']
                number = data['number']
                role = SEND + RECEIVE

            # see if there is a parent channel we are adding a delegate for
            channel = self.request.REQUEST.get('channel', None)
            if channel:
                # make sure they own it
                channel = self.request.user.get_org().channels.filter(pk=channel).first()

            config = {SEND_URL: data['url'], SEND_METHOD: data['method']}
            self.object = Channel.add_config_external_channel(org, self.request.user, country, number, EXTERNAL,
                                                              config, role=role, parent=channel)

            # make sure all contacts added before the channel are normalized
            self.object.ensure_normalized_contacts()

            return super(ChannelCRUDL.ClaimExternal, self).form_valid(form)

    class ClaimAuthenticatedExternal(OrgPermsMixin, SmartFormView):
        class AEClaimForm(forms.Form):
            country = forms.ChoiceField(choices=ALL_COUNTRIES, label=_("Country"),
                                        help_text=_("The country this phone number is used in"))
            number = forms.CharField(max_length=14, min_length=1, label=_("Number"),
                                     help_text=_("The phone number or short code you are connecting with country code. ex: +250788123124"))
            username = forms.CharField(label=_("Username"),
                                       help_text=_("The username provided by the provider to use their API"))
            password = forms.CharField(label=_("Password"),
                                       help_text=_("The password provided by the provider to use their API"))

            def clean_number(self):
                number = self.data['number']
                if number and number[0] != '+':
                    number = '+' + number

                try:
                    cleaned = phonenumbers.parse(number, None)
                    return phonenumbers.format_number(cleaned, phonenumbers.PhoneNumberFormat.E164)
                except:
                    raise forms.ValidationError(_("Invalid phone number, please include the country code. ex: +250788123123"))

        title = "Connect External Service"
        fields = ('country', 'number', 'username', 'password')
        form_class = AEClaimForm
        success_url = "id@channels.channel_configuration"
        channel_type = "AE"

        def get_submitted_country(self, data):
            return data['country']

        def form_valid(self, form):
            org = self.request.user.get_org()

            if not org:  # pragma: no cover
                raise Exception("No org for this user, cannot claim")

            data = form.cleaned_data
            self.object = Channel.add_authenticated_external_channel(org, self.request.user,
                                                                     self.get_submitted_country(data),
                                                                     data['number'], data['username'],
                                                                     data['password'], self.channel_type)

            # make sure all contacts added before the channel are normalized
            self.object.ensure_normalized_contacts()

            return super(ChannelCRUDL.ClaimAuthenticatedExternal, self).form_valid(form)

    class ClaimInfobip(ClaimAuthenticatedExternal):
        title = _("Connect Infobip")
        channel_type = INFOBIP

    class ClaimVerboice(ClaimAuthenticatedExternal):
        class VerboiceClaimForm(forms.Form):
            country = forms.ChoiceField(choices=ALL_COUNTRIES, label=_("Country"),
                                        help_text=_("The country this phone number is used in"))
            number = forms.CharField(max_length=14, min_length=1, label=_("Number"),
                                     help_text=_("The phone number with country code or short code you are connecting. ex: +250788123124 or 15543"))
            username = forms.CharField(label=_("Username"),
                                       help_text=_("The username provided by the provider to use their API"))
            password = forms.CharField(label=_("Password"),
                                       help_text=_("The password provided by the provider to use their API"))
            channel = forms.CharField(label=_("Channel Name"),
                                           help_text=_("The Verboice channel that will be handling your calls"))

        title = _("Connect Verboice")
        channel_type = VERBOICE
        form_class = VerboiceClaimForm
        fields = ('country', 'number', 'username', 'password', 'channel')

        def form_valid(self, form):
            org = self.request.user.get_org()

            if not org:  # pragma: no cover
                raise Exception(_("No org for this user, cannot claim"))

            data = form.cleaned_data
            self.object = Channel.add_config_external_channel(org, self.request.user,
                                                              data['country'], data['number'], VERBOICE,
                                                              dict(username=data['username'],
                                                                   password=data['password'],
                                                                   channel=data['channel']),
                                                              role=CALL+ANSWER)

            # make sure all contacts added before the channel are normalized
            self.object.ensure_normalized_contacts()

            return super(ChannelCRUDL.ClaimAuthenticatedExternal, self).form_valid(form)

    class ClaimHub9(ClaimAuthenticatedExternal):
        title = _("Connect Hub9")
        channel_type = HUB9
        readonly = ('country',)

        def get_country(self, obj):
            return "Indonesia"

        def get_submitted_country(self, data):
            return "ID"

    class ClaimShaqodoon(ClaimAuthenticatedExternal):
        class ShaqodoonForm(forms.Form):
            country = forms.ChoiceField(choices=ALL_COUNTRIES, label=_("Country"),
                                        help_text=_("The country this phone number is used in"))
            number = forms.CharField(max_length=14, min_length=1, label=_("Number"),
                                     help_text=_("The short code you are connecting with."))
            url = forms.URLField(label=_("URL"),
                                 help_text=_("The url provided to deliver messages"))
            username = forms.CharField(label=_("Username"),
                                       help_text=_("The username provided to use their API"))
            password = forms.CharField(label=_("Password"),
                                       help_text=_("The password provided to use their API"))
            key = forms.CharField(label=_("Key"),
                                  help_text=_("The key provided to sign requests"))

        title = _("Connect Shaqodoon")
        channel_type = SHAQODOON
        readonly = ('country',)
        form_class = ShaqodoonForm
        fields = ('country', 'number', 'url', 'username', 'password', 'key')

        def get_country(self, obj):
            return "Somalia"

        def get_submitted_country(self, data):
            return 'SO'

        def form_valid(self, form):
            org = self.request.user.get_org()

            if not org:  # pragma: no cover
                raise Exception(_("No org for this user, cannot claim"))

            data = form.cleaned_data
            self.object = Channel.add_config_external_channel(org, self.request.user,
                                                              'SO', data['number'], SHAQODOON,
                                                              dict(key=data['key'],
                                                                   send_url=data['url'],
                                                                   username=data['username'],
                                                                   password=data['password']))

            # make sure all contacts added before the channel are normalized
            self.object.ensure_normalized_contacts()

            return super(ChannelCRUDL.ClaimAuthenticatedExternal, self).form_valid(form)


    class ClaimVumi(ClaimAuthenticatedExternal):
        class VumiClaimForm(forms.Form):
            country = forms.ChoiceField(choices=ALL_COUNTRIES, label=_("Country"),
                                        help_text=_("The country this phone number is used in"))
            number = forms.CharField(max_length=14, min_length=1, label=_("Number"),
                                     help_text=_("The phone number with country code or short code you are connecting. ex: +250788123124 or 15543"))
            account_key = forms.CharField(label=_("Account Key"),
                                          help_text=_("Your Vumi account key as found under Account -> Details"))
            conversation_key = forms.CharField(label=_("Conversation Key"),
                                               help_text=_("The key for your Vumi conversation, can be found in the URL"))
            transport_name = forms.CharField(label=_("Transport Name"),
                                             help_text=_("The name of the Vumi transport you will use to send and receive messages"))

        title = _("Connect Vumi")
        channel_type = VUMI
        form_class = VumiClaimForm
        fields = ('country', 'number', 'account_key', 'conversation_key', 'transport_name')

        def form_valid(self, form):
            org = self.request.user.get_org()

            if not org:  # pragma: no cover
                raise Exception(_("No org for this user, cannot claim"))

            data = form.cleaned_data
            self.object = Channel.add_config_external_channel(org, self.request.user,
                                                              data['country'], data['number'], VUMI,
                                                              dict(account_key=data['account_key'],
                                                                   access_token=str(uuid4()),
                                                                   transport_name=data['transport_name'],
                                                                   conversation_key=data['conversation_key']))

            # make sure all contacts added before the channel are normalized
            self.object.ensure_normalized_contacts()

            return super(ChannelCRUDL.ClaimAuthenticatedExternal, self).form_valid(form)


    class ClaimAfricasTalking(OrgPermsMixin, SmartFormView):
        class ATClaimForm(forms.Form):
            shortcode = forms.CharField(max_length=6, min_length=1,
                                        help_text=_("Your short code on Africa's Talking"))
            username = forms.CharField(max_length=32,
                                       help_text=_("Your username on Africa's Talking"))
            api_key = forms.CharField(max_length=64,
                                      help_text=_("Your api key, should be 64 characters"))

        title = _("Connect Africa's Talking Account")
        fields = ('shortcode', 'username', 'api_key')
        form_class = ATClaimForm
        success_url = "id@channels.channel_configuration"

        def form_valid(self, form):
            org = self.request.user.get_org()

            if not org: # pragma: no cover
                raise Exception(_("No org for this user, cannot claim"))

            data = form.cleaned_data
            self.object = Channel.add_africas_talking_channel(org, self.request.user,
                                                              phone=data['shortcode'], username=data['username'], api_key=data['api_key'])

            # make sure all contacts added before the channel are normalized
            self.object.ensure_normalized_contacts()

            return super(ChannelCRUDL.ClaimAfricasTalking, self).form_valid(form)

    class Configuration(OrgPermsMixin, SmartReadView):

        def get_context_data(self, **kwargs):
            context = super(ChannelCRUDL.Configuration, self).get_context_data(**kwargs)

            # if this is an external channel, build an example URL
            if self.object.channel_type == EXTERNAL:
                context['example_url'] = Channel.build_send_url(self.object.config_json()[SEND_URL],
                          {'to': '+250788123123', 'text': "Love is patient. Love is kind", 'from': self.object.address, 'id': '1241244', 'channel': str(self.object.id)})

            context['domain'] = settings.HOSTNAME

            return context

    class ClaimAndroid(OrgPermsMixin, SmartFormView):
        title = _("Register Android Phone")
        fields = ('claim_code', 'phone_number')
        form_class = ClaimForm
        title = _("Claim Channel")

        def get_success_url(self):
            return "%s?success" % reverse('public.public_welcome')

        def form_valid(self, form):
            org = self.request.user.get_org()

            if not org:  # pragma: no cover
                raise Exception(_("No org for this user, cannot claim"))

            self.object = Channel.objects.filter(claim_code=self.form.cleaned_data['claim_code'])[0]

            country = self.object.country
            if not country:
                country = Channel.derive_country_from_phone(self.form.cleaned_data['phone_number'])

            # get all other channels with a country
            other_channels = org.channels.filter(is_active=True).exclude(pk=self.object.pk)
            with_countries = other_channels.exclude(country=None).exclude(country="")

            # are there any that have a different country?
            other_countries = with_countries.exclude(country=country).first()
            if other_countries:
                form._errors['claim_code'] = form.error_class([_("Sorry, you can only add numbers for the same country (%s)" % other_countries.country)])
                return self.form_invalid(form)

            # clear any channels that are dupes of this gcm/uuid pair for this org
            for dupe in Channel.objects.filter(gcm_id=self.object.gcm_id, uuid=self.object.uuid, org=org).exclude(
                                               pk=self.object.pk).exclude(gcm_id=None).exclude(uuid=None):
                dupe.is_active = False
                dupe.gcm_id = None
                dupe.uuid = None
                dupe.claim_code = None
                dupe.save()

            analytics.track(self.request.user.username, 'temba.channel_create')

            self.object.claim(org, self.form.cleaned_data['phone_number'], self.request.user)
            self.object.save()

            # make sure all contacts added before the channel are normalized
            self.object.ensure_normalized_contacts()

            # trigger a sync
            self.object.trigger_sync()

            return super(ChannelCRUDL.ClaimAndroid, self).form_valid(form)

        def derive_org(self):
            user = self.request.user
            org = None

            if not user.is_anonymous():
                org = user.get_org()

            org_id = self.request.session.get('org_id', None)
            if org_id:
                org = Org.objects.get(pk=org_id)

            return org

    class ClaimTwitter(OrgPermsMixin, SmartTemplateView):

        @non_atomic_when_eager
        def dispatch(self, *args, **kwargs):
            """
            Decorated with @non_atomic_when_eager so that channel object is always committed to database before Mage
            tries to access it
            """
            return super(ChannelCRUDL.ClaimTwitter, self).dispatch(*args, **kwargs)

        def pre_process(self, *args, **kwargs):
            response = super(ChannelCRUDL.ClaimTwitter, self).pre_process(*args, **kwargs)

            api_key = settings.TWITTER_API_KEY
            api_secret = settings.TWITTER_API_SECRET
            oauth_token = self.request.session.get(SESSION_TWITTER_TOKEN, None)
            oauth_token_secret = self.request.session.get(SESSION_TWITTER_SECRET, None)
            oauth_verifier = self.request.REQUEST.get('oauth_verifier', None)

            # if we have all oauth values, then we be returning from an authorization callback
            if oauth_token and oauth_token_secret and oauth_verifier:
                twitter = Twython(api_key, api_secret, oauth_token, oauth_token_secret)
                final_step = twitter.get_authorized_tokens(oauth_verifier)
                screen_name = final_step['screen_name']
                handle_id = final_step['user_id']
                oauth_token = final_step['oauth_token']
                oauth_token_secret = final_step['oauth_token_secret']

                org = self.request.user.get_org()
                if not org:  # pragma: no cover
                    raise Exception(_("No org for this user, cannot claim"))

                channel = Channel.add_twitter_channel(org, self.request.user, screen_name, handle_id, oauth_token, oauth_token_secret)
                del self.request.session[SESSION_TWITTER_TOKEN]
                del self.request.session[SESSION_TWITTER_SECRET]

                return redirect(reverse('channels.channel_read', kwargs=dict(pk=channel.id)))

            return response

        def get_context_data(self, **kwargs):
            context = super(ChannelCRUDL.ClaimTwitter, self).get_context_data(**kwargs)

            # generate temp OAuth token and secret
            twitter = Twython(settings.TWITTER_API_KEY, settings.TWITTER_API_SECRET)
            callback_url = self.request.build_absolute_uri(reverse('channels.channel_claim_twitter'))
            auth = twitter.get_authentication_tokens(callback_url=callback_url)

            # put in session for when we return from callback
            self.request.session[SESSION_TWITTER_TOKEN] = auth['oauth_token']
            self.request.session[SESSION_TWITTER_SECRET] = auth['oauth_token_secret']

            context['twitter_auth_url'] = auth['auth_url']
            return context

    class List(OrgPermsMixin, SmartListView):
        title = _("Channels")
        fields = ('name', 'address', 'last_seen')
        search_fields = ('name', 'number', 'org__created_by__email')

        def get_queryset(self, **kwargs):
            queryset = super(ChannelCRUDL.List, self).get_queryset(**kwargs)

            if not self.request.user.is_superuser:
                queryset = queryset.filter(org__administrators__in=[self.request.user])
            return queryset

        def pre_process(self, *args, **kwargs):
            # superuser sees things as they are
            if self.request.user.is_superuser:
                return super(ChannelCRUDL.List, self).pre_process(*args, **kwargs)

            # everybody else goes to a different page depending how many channels there are
            org = self.request.user.get_org()
            channels = list(Channel.objects.filter(org=org, is_active=True).exclude(org=None))

            if len(channels) == 0:
                return HttpResponseRedirect(reverse('channels.channel_claim'))
            elif len(channels) == 1:
                return HttpResponseRedirect(reverse('channels.channel_read', args=[channels[0].id]))
            else:
                return super(ChannelCRUDL.List, self).pre_process(*args, **kwargs)

        def get_name(self, obj):
            return obj.get_name()

        def get_address(self, obj):
            return obj.address if obj.address else _("Unknown")

    class SearchNumbers(OrgPermsMixin, SmartFormView):
        class SearchNumbersForm(forms.Form):
            area_code = forms.CharField(max_length=3, min_length=3, required=False,
                                        help_text=_("The area code you want to search for a new number in"))
            country = forms.ChoiceField(choices=TWILIO_SEARCH_COUNTRIES)

        form_class = SearchNumbersForm

        def form_invalid(self, *args, **kwargs):
            return HttpResponse(json.dumps([]))

        def search_available_numbers(self, client, **kwargs):
            available_numbers = []

            kwargs['type'] = 'local'
            try:
                available_numbers += client.phone_numbers.search(**kwargs)
            except TwilioRestException:
                pass

            kwargs['type'] = 'mobile'
            try:
                available_numbers += client.phone_numbers.search(**kwargs)
            except TwilioRestException:
                pass

            return available_numbers

        def form_valid(self, form, *args, **kwargs):
            org = self.request.user.get_org()
            client = org.get_twilio_client()
            data = form.cleaned_data

            # if the country is not US or CANADA list using contains instead of area code
            if not data['area_code']:
                available_numbers = self.search_available_numbers(client, country=data['country'])
            elif data['country'] in ['CA', 'US']:
                available_numbers = self.search_available_numbers(client, area_code=data['area_code'], country=data['country'])
            else:
                available_numbers = self.search_available_numbers(client, contains=data['area_code'], country=data['country'])

            numbers = []

            for number in available_numbers:
                numbers.append(phonenumbers.format_number(phonenumbers.parse(number.phone_number, None),
                                                          phonenumbers.PhoneNumberFormat.INTERNATIONAL))

            return HttpResponse(json.dumps(numbers))


    class ClaimNumber(OrgPermsMixin, SmartFormView):
        class ClaimNumberForm(forms.Form):

            country = forms.ChoiceField(choices=TWILIO_SUPPORTED_COUNTRIES)
            phone_number = forms.CharField(help_text=_("The phone number being added"))

            def clean_phone_number(self):
                phone = self.cleaned_data['phone_number']
                phone = phonenumbers.parse(phone, self.cleaned_data['country'])
                return phonenumbers.format_number(phone, phonenumbers.PhoneNumberFormat.E164)

        form_class = ClaimNumberForm

        def pre_process(self, *args, **kwargs):
            org = self.request.user.get_org()
            try:
                client = org.get_twilio_client()
            except:
                client = None

            if client:
                return None
            else:
                return HttpResponseRedirect(reverse('channels.channel_claim'))

        def get_context_data(self, **kwargs):
            context = super(ChannelCRUDL.ClaimNumber, self).get_context_data(**kwargs)

            org = self.request.user.get_org()

            try:
                context['account_numbers'] = self.get_existing_numbers(org)
            except Exception as e:
                context['account_numbers'] = []
                context['error'] = str(e)

            context['search_url'] = self.get_search_url()
            context['claim_url'] = self.get_claim_url()

            context['search_countries'] = self.get_search_countries()
            context['supported_country_iso_codes'] = self.get_supported_country_iso_codes()

            return context

        def get_search_countries(self):
            search_countries = []

            for country in self.get_search_countries_tuple():
                search_countries.append(dict(key=country[0], label=country[1]))

            return search_countries

        def get_supported_country_iso_codes(self):
            supported_country_iso_codes = []

            for country in self.get_supported_countries_tuple():
                supported_country_iso_codes.append(country[0])

            return supported_country_iso_codes

        def get_search_countries_tuple(self):
            return TWILIO_SEARCH_COUNTRIES

        def get_supported_countries_tuple(self):
            return TWILIO_SUPPORTED_COUNTRIES

        def get_search_url(self):
            return reverse('channels.channel_search_numbers')

        def get_claim_url(self):
            return reverse('channels.channel_claim_number')

        def get_existing_numbers(self, org):
            client = org.get_twilio_client()
            if client:
                twilio_account_numbers = client.phone_numbers.list()

            numbers = []
            for number in twilio_account_numbers:
                parsed = phonenumbers.parse(number.phone_number, None)
                numbers.append(dict(number=phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.INTERNATIONAL),
                                    country=region_code_for_number(parsed)))
            return numbers

        def is_valid_country(self, country_code):
            return country_code in TWILIO_SUPPORTED_COUNTRY_CODES

        def claim_number(self, user, phone_number, country):
            analytics.track(user.username, 'temba.channel_claim_twilio', properties=dict(number=phone_number))

            # add this channel
            channel = Channel.add_twilio_channel(user.get_org(),
                                                 user,
                                                 phone_number,
                                                 country)

            return channel

        def form_valid(self, form, *args, **kwargs):

            # must have an org
            org = self.request.user.get_org()
            if not org:
                form._errors['upgrade'] = True
                form._errors['phone_number'] = form.error_class([_("Sorry, you need to have an organization to add numbers. "
                                                                   "You can still test things out for free using an Android phone.")])
                return self.form_invalid(form)

            data = form.cleaned_data

            # can't add a channel from a different country
            other_countries = org.channels.exclude(country=None).exclude(is_active=False).exclude(country=data['country']).first()
            if other_countries:
                form._errors['phone_number'] = form.error_class([_("Sorry, you can only add numbers for the same country (%s)" % other_countries.country)])
                return self.form_invalid(form)

            phone = phonenumbers.parse(data['phone_number'])
            if not self.is_valid_country(phone.country_code):
                form._errors['phone_number'] = form.error_class([_("Sorry, the number you chose is not supported. "
                                                                   "You can still deploy in any country using your own SIM card and an Android phone.")])
                return self.form_invalid(form)

            # don't add the same number twice to the same account
            existing = org.channels.filter(is_active=True, address=data['phone_number']).first()
            if existing:
                form._errors['phone_number'] = form.error_class([_("That number is already connected (%s)" % data['phone_number'])])
                return self.form_invalid(form)

            existing = Channel.objects.filter(is_active=True, address=data['phone_number']).first()
            if existing:
                form._errors['phone_number'] = form.error_class([_("That number is already connected to another account - %s (%s)" % (existing.org, existing.created_by.username))])
                return self.form_invalid(form)

            # try to claim the number from twilio
            try:
                channel = self.claim_number(self.request.user,
                                            data['phone_number'], data['country'])

                # make sure all contacts added before the channel are normalized
                channel.ensure_normalized_contacts()

                return HttpResponseRedirect('%s?success' % reverse('public.public_welcome'))
            except Exception as e:
                import traceback
                traceback.print_exc(e)
                if e.message:
                    form._errors['phone_number'] = form.error_class([unicode(e.message)])
                else:
                    form._errors['phone_number'] = _("An error occurred connecting your Twilio number, try removing your "
                                                     "Twilio account, reconnecting it and trying again.")
                return self.form_invalid(form)

    class ClaimNexmo(ClaimNumber):
        class ClaimNexmoForm(forms.Form):
            country = forms.ChoiceField(choices=NEXMO_SUPPORTED_COUNTRIES)
            phone_number = forms.CharField(help_text=_("The phone number being added"))

            def clean_phone_number(self):
                if not self.cleaned_data.get('country', None):
                    raise ValidationError(_("That number is not currently supported."))

                phone = self.cleaned_data['phone_number']
                phone = phonenumbers.parse(phone, self.cleaned_data['country'])

                return phonenumbers.format_number(phone, phonenumbers.PhoneNumberFormat.E164)

        form_class = ClaimNexmoForm

        template_name = 'channels/channel_claim_nexmo.html'

        def pre_process(self, *args, **kwargs):
            org = Org.objects.get(pk=self.request.user.get_org().pk)
            try:
                client = org.get_nexmo_client()
            except:
                client = None

            if client:
                return None
            else:
                return HttpResponseRedirect(reverse('channels.channel_claim'))

        def is_valid_country(self, country_code):
            return country_code in NEXMO_SUPPORTED_COUNTRY_CODES

        def get_search_url(self):
            return reverse('channels.channel_search_nexmo')

        def get_claim_url(self):
            return reverse('channels.channel_claim_nexmo')

        def get_supported_countries_tuple(self):
            return NEXMO_SUPPORTED_COUNTRIES

        def get_search_countries_tuple(self):
            return NEXMO_SUPPORTED_COUNTRIES

        def get_existing_numbers(self, org):
            client = org.get_nexmo_client()
            if client:
                account_numbers = client.get_numbers()

            numbers = []
            for number in account_numbers:
                if number['type'] == 'mobile-shortcode':
                    phone_number = number['msisdn']
                else:
                    parsed = phonenumbers.parse(number['msisdn'], number['country'])
                    phone_number = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.INTERNATIONAL)
                numbers.append(dict(number=phone_number, country=number['country']))

            return numbers

        def claim_number(self, user, phone_number, country):
            analytics.track(user.username, 'temba.channel_claim_nexmo', dict(number=phone_number))

            # add this channel
            channel = Channel.add_nexmo_channel(user.get_org(),
                                                user,
                                                country,
                                                phone_number)

            return channel

    class SearchNexmo(SearchNumbers):
        class SearchNexmoForm(forms.Form):
            area_code = forms.CharField(max_length=3, min_length=3, required=False,
                                        help_text=_("The area code you want to search for a new number in"))
            country = forms.ChoiceField(choices=NEXMO_SUPPORTED_COUNTRIES)

        form_class = SearchNexmoForm

        def form_valid(self, form, *args, **kwargs):
            org = self.request.user.get_org()
            client = org.get_nexmo_client()
            data = form.cleaned_data

            # if the country is not US or CANADA list using contains instead of area code
            try:
                available_numbers = client.search_numbers(data['country'], data['area_code'])
                numbers = []

                for number in available_numbers:
                    numbers.append(phonenumbers.format_number(phonenumbers.parse(number['msisdn'], data['country']),
                                                              phonenumbers.PhoneNumberFormat.INTERNATIONAL))

                return HttpResponse(json.dumps(numbers))
            except Exception as e:
                return HttpResponse(json.dumps(error=str(e)))


class ChannelLogCRUDL(SmartCRUDL):
    model = ChannelLog
    actions = ('list', 'read')

    class List(OrgPermsMixin, SmartListView):
        fields = ('channel', 'description', 'created_on')
        link_fields = ('channel', 'description', 'created_on')

        def derive_queryset(self, **kwargs):
            channel = Channel.objects.get(pk=self.request.REQUEST['channel'])
            errors = ChannelLog.objects.filter(msg__channel=channel,
                                               msg__channel__org=self.request.user.get_org).order_by('-created_on')
            return errors

        def get_context_data(self, **kwargs):
            context = super(ChannelLogCRUDL.List, self).get_context_data(**kwargs)
            context['channel'] = Channel.objects.get(pk=self.request.REQUEST['channel'])
            return context

    class Read(ChannelCRUDL.AnonMixin, SmartReadView):
        fields = ('description', 'created_on')

        def derive_queryset(self, **kwargs):
            queryset = super(ChannelLogCRUDL.Read, self).derive_queryset(**kwargs)
            return queryset.filter(msg__channel__org=self.request.user.get_org).order_by('-created_on')
