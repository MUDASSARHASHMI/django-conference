from datetime import date, datetime
from decimal import Decimal
from calendar import monthrange
import re
import stripe

from django import forms
from django.apps import apps
from django.utils.safestring import mark_safe
from django.utils.encoding import force_unicode
from django.db.models import get_model
from django.conf import settings

from django_conference import settings as conf_settings
from django_conference.models import (Meeting, Paper, Session, SessionCadre,
    RegistrationDonation, Registration, RegistrationExtra,
    RegistrationGuest, RegistrationOption, PaperPresenter)


class SessionsWidget(forms.CheckboxSelectMultiple):
    """
    Widget for representing all the sessions associated with a certain
    time slot. Sessions titles are displayed in a list, with complete details
    on the session below the title.
    """
    def render(self, name, value, attrs=None, choices=()):
        if value is None:
            value = []
        has_id = attrs and 'id' in attrs
        final_attrs = self.build_attrs(attrs, name=name)
        values = set([force_unicode(v) for v in value])
        # since all the sessions have the same time slot,
        # use the first in the list to get the description
        session = Session.objects.get(pk=self.choices[0][1])
        description = session.get_time_slot_string()
        expand_img = '<img src="%sdjango_conference/img/expand.gif" alt="Expand list"/>' % (
            settings.STATIC_URL)
        output = [u"""
            <div class="session_list">
                <h3>%s %s</h3>
                <ol>""" % (expand_img, description)]
        for (i, choice) in enumerate(self.choices):
            session_id = choice[0]
            if has_id:
                final_attrs = dict(final_attrs, id='%s_%s' % (attrs['id'], i))
            session = Session.objects.get(pk=session_id)
            cb = forms.CheckboxInput(final_attrs,
                check_test=lambda v: v in values)
            rendered_cb = cb.render(name, unicode(session_id))
            output.append(u"""
                    <li>
                        <h4>%s<span>%s</span></h4>
                        <div class="session_details">"""
                % (rendered_cb, unicode(session)))
            cadre_dict = {
                'Chair': session.chairs,
                'Organizer': session.organizers,
                'Commentator': session.commentators,
            }
            for desc, cadre in cadre_dict.items():
                if not cadre.count():
                    continue
                if cadre.count() == 1:
                    cadre_name = unicode(cadre.all()[0])
                    output.append(u'%s: %s<br/>' % (desc, cadre_name))
                elif cadre.count() > 1:
                    output.append(u'%ss:<ul>' % desc)
                    output.extend([u'<li>%s</li>' %
                        unicode(o) for o in cadre.all()])
                    output.append(u'</ul>')
            if session.papers:
                output.append(u"""
                            <strong>%s Papers</strong>
                            <ul class="papers">""" % expand_img)
                output.extend([u'<li><em>%s</em>, %s</li>' %
                    (unicode(p), p.get_presenter())
                    for p in session.papers.all()])
                output.append(u'</ul>')
            output.append(u"""
                        </div>
                    </li>""")
        output.append(u"""
                </ol>
            </div>""")
        return mark_safe(u'\n'.join(output))


class MeetingSessions(forms.Form):
    """
    Form for selecting meeting sessions. All fields are dynamically generated
    by querying for the sessions associated with a given meeting.
    """
    def __init__(self, meeting, *args, **kwargs):
        super(MeetingSessions, self).__init__(*args, **kwargs)
        self.meeting = meeting
        self.set_session_fields()

    def set_session_fields(self):
        # adds multi-select fields for choosing which sessions to attend,
        # with one field for each (start_time, stop_time) combo
        meeting_sessions = self.meeting.sessions.filter(accepted=True)
        time_slots = (meeting_sessions.filter(accepted=1).distinct()
                        .values_list("start_time", "stop_time")
                        .order_by('start_time', 'stop_time'))
        for i, time_slot in enumerate(time_slots):
            sessions = meeting_sessions.filter(start_time=time_slot[0],
                stop_time=time_slot[1])
            choices = [(s.pk, s.pk) for s in sessions]
            field_name = "sessions_%i" % i
            self.fields[field_name] = forms.MultipleChoiceField(label="",
                choices=choices, required=False, widget=SessionsWidget)

    def get_sessions(self):
        clean = self.clean()
        # go through and split off session-related values into a separate list,
        # since they have to be combined together
        sessions = []
        for item in clean.keys():
            if item.startswith("sessions_"):
                if clean[item]:
                    sessions.append(clean[item])
                clean.pop(item)
        # flatten list
        sessions = sum(sessions, [])
        return [Session.objects.get(pk=int(x)) for x in sessions]


class MeetingRegister(forms.Form):
    """
    Main form for meeting registrations.
    """
    type = forms.ChoiceField(choices=[], required=True,
        label="Registration Type")
    guest_first_name = forms.CharField(required=False, max_length=45)
    guest_last_name = forms.CharField(required=False, max_length=45)
    special_needs = forms.CharField(required=False, widget=forms.Textarea)

    def __init__(self, meeting, *args, **kwargs):
        super(MeetingRegister, self).__init__(*args, **kwargs)
        self.meeting = meeting
        self.set_type_field()

    def set_type_field(self):
        """
        Generate list of available registration type (e.g. HSS Member, student)
        based on current date and deadlines associated with the meeting.
        """
        early_reg_passed = date.today() > self.meeting.early_reg_deadline
        TYPES = [("", "Please select")] + \
                [(x.id, x.option_name+"\t$"+
                  str(x.regular_price if early_reg_passed else x.early_price))
                 for x in self.meeting.regoptions.filter(admin_only=False)]
        self.fields['type'].choices = TYPES

    def get_guest(self):
        """
        Returns unsaved RegistrationGuest object if user entered something
        for the guest_first_name field, else returns None.
        """
        clean = self.clean()
        if not clean.get('guest_first_name'):
            return None
        return RegistrationGuest(first_name=clean['guest_first_name'],
            last_name=clean['guest_last_name'])

    def get_registration(self, registrant):
        """
        Returns unsaved Registration object corresponding to cleaned form data
        and the given registrant.
        """
        clean = self.clean()
        user_model = apps.get_model(conf_settings.DJANGO_CONFERENCE_USER_MODEL)
        reg_username = conf_settings.DJANGO_CONFERENCE_ONLINE_REG_USERNAME
        entered_by = user_model.objects.get_by_natural_key(reg_username)
        kwargs = {
            'meeting': self.meeting,
            'type': RegistrationOption.objects.get(id=clean['type']),
            'special_needs': clean.get('special_needs', ''),
            'date_entered': datetime.today(),
            'payment_type': 'cc',
            'registrant': registrant,
            'entered_by': entered_by,
        }
        return Registration(**kwargs)


class MeetingExtras(forms.Form):
    """
    Form for fixed-price meeting extra (e.g. abstracts). All fields are
    dynamically generated from the MeetingExtra model.
    """
    def __init__(self, meeting, *args, **kwargs):
        super(MeetingExtras, self).__init__(*args, **kwargs)
        self.meeting = meeting
        extras = meeting.extras.filter(admin_only=False)
        for extra in extras:
            field = extra.extra_type
            if extra.max_quantity == 1:
                self.fields[field.name] = forms.BooleanField(required=False,
                    label=field.label, help_text=extra.help_text)
            else:
                self.fields[field.name] = forms.IntegerField(required=False,
                    initial=0, min_value=0, max_value=extra.max_quantity,
                    label=field.label, help_text=extra.help_text)

    def get_extras(self, registrant):
        """
        Returns list of RegistrationExtra objects
        """
        clean = self.clean()
        extras = []
        for name, qty in clean.items():
            if qty is True:
                qty = 1
            if not qty:
                continue
            extra = self.meeting.extras.get(extra_type__name=name)
            price = None
            reg_extra = RegistrationExtra(extra=extra, quantity=qty,
                price=price)
            extras.append(reg_extra)
        return extras


class MeetingDonations(forms.Form):
    """
    Form for donations that registrants can make. All fields are
    dynamically generated from the MeetingDonation model.
    """
    def __init__(self, meeting, *args, **kwargs):
        super(MeetingDonations, self).__init__(*args, **kwargs)
        self.meeting = meeting
        donations = meeting.donations.all()
        for donation in donations:
            field = donation.donate_type
            self.fields[field.name] = forms.DecimalField(required=False,
                widget = self.MoneyWidget(), decimal_places=2,
                min_value=Decimal("0.0"), max_digits=6,
                label=field.label, help_text=donation.help_text, initial=0)

    class MoneyWidget(forms.TextInput):
        def render(self, *args, **kwargs):
            return '$' + super(self.__class__, self).render(*args, **kwargs)

    def get_donations(self):
        """
        Returns list of RegistrationDonation objects
        """
        clean = self.clean()
        donations = []
        for name, total in clean.items():
            if not total:
                continue
            total = Decimal(total)
            donate_type = self.meeting.donations.get(donate_type__name=name)
            donation = RegistrationDonation(total=total,
                donate_type=donate_type)
            donations.append(donation)
        return donations


class SessionCadreForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        """
        Special form for the SessionCadre model that accepts a "optional"
        keyword, which indicates that validation should only happen
        if the user enters something in
        """
        optional = kwargs.pop('optional', True)
        super(SessionCadreForm, self).__init__(*args, **kwargs)
        if optional:
            for f in self.fields.values():
                f.required = False

    def has_entered_info(self):
        data = self.cleaned_data
        return any([data.get('first_name'), data.get('last_name'),
            data.get('mi'), data.get('email'), data.get('institution')])

    def clean(self):
        clean = super(SessionCadreForm, self).clean()
        if self.has_entered_info():
            #i.e. User Entered Some Data
            #lets ensure all necessary data is present
            if not all([clean.get('first_name'), clean.get('last_name'),
                clean.get('email'), clean.get('institution')]):
                err = "Please fill in all the first name, last name, email,"+\
                      " and institution fields for this person."
                raise forms.ValidationError(err)
        return clean

    class Meta:
        model = SessionCadre
        fields = ["first_name", "mi", "last_name", "gender", "type", "email",
            "institution"]


class AbstractForm(forms.ModelForm):
    """
    Base form for models that contain abstracts
    """
    def __init__(self, *args, **kwargs):
        super(AbstractForm, self).__init__(*args, **kwargs)
        max_words = conf_settings.DJANGO_CONFERENCE_ABSTRACT_MAX_WORDS
        if max_words > 0:
            self.fields['abstract'].help_text = "%i words maximum" % max_words

    def clean_abstract(self):
        data = self.cleaned_data['abstract']
        num_words = len(data.split())
        max_words = conf_settings.DJANGO_CONFERENCE_ABSTRACT_MAX_WORDS
        if num_words > max_words:
            message = "Abstract can contain a maximum of %i words. "+\
                      "You supplied %i words."
            raise forms.ValidationError(message % (max_words, num_words))
        return data


class PaperPresenterForm(forms.ModelForm):
    header = "Presenter Information"
    class Meta:
        model = PaperPresenter
        fields = ['first_name', 'last_name', 'email', 'gender', 'birth_year',
            'status', 'time_periods', 'regions', 'subjects']


class PaperForm(AbstractForm):
    header = "Paper/Poster Information"

    class Meta:
        model = Paper
        exclude = ['creation_time', 'submitter', 'presenter', 'accepted', 'sessions']

    def save(self, submitter, presenter=None, commit=True):
        self.instance.submitter = submitter
        if presenter:
            self.instance.presenter = presenter
        return super(PaperForm, self).save(commit)


class SessionForm(AbstractForm):
    NUM_PAPERS_RANGE = [(x, x) for x in range(3, 11)]
    num_papers = forms.ChoiceField(choices=NUM_PAPERS_RANGE, required=True,
        label="Paper Abstracts")

    class Meta:
        model = Session
        fields = ['title', 'abstract', 'notes', 'num_papers']

    def save(self, meeting, submitter, commit=True):
        self.instance.meeting = meeting
        self.instance.submitter = submitter
        return super(SessionForm, self).save(commit)


class StripeTextInput(forms.TextInput):
    """
    This is a version of forms.widgets.TextInput that doesn't render the input
    with the "name" or "value" attributes. We don't want to have the "name"
    attribute because that will cause CC details to be POSTed to us when the
    form is submitted.
    """
    def __init__(self, stripe_field_name, attrs=None):
        self.stripe_field_name = stripe_field_name
        super(StripeTextInput, self).__init__(attrs)

    def render(self, name, value, attrs=None):
        extra = {'type': self.input_type, 'data-stripe': self.stripe_field_name}
        final_attrs = self.build_attrs(attrs, **extra)
        return mark_safe(u'<input%s />' % forms.utils.flatatt(final_attrs))


class StripeSelect(forms.Select):
    """
    Same as StripeTextInput, except for <select>s
    """
    def __init__(self, stripe_field_name, attrs=None):
        self.stripe_field_name = stripe_field_name
        super(StripeSelect, self).__init__(attrs)

    def render(self, name, value, attrs=None, choices=()):
        extra = {'data-stripe': self.stripe_field_name}
        final_attrs = self.build_attrs(attrs, **extra)
        output = [u'<select%s>' % forms.utils.flatatt(final_attrs)]
        output.append(self.render_options(choices, []))
        output.append(u'</select>')
        return mark_safe(u'\n'.join(output))


class CCExpWidget(forms.MultiWidget):
    """ Widget containing two select boxes for selecting the month and year"""
    def decompress(self, value):
        return [value.month, value.year] if value else [None, None]

    def format_output(self, rendered_widgets):
        html = u' / '.join(rendered_widgets)
        return u'<span style="white-space: nowrap">%s</span>' % html


class CCExpField(forms.MultiValueField):
    EXP_MONTH = [(str(x).rjust(2, "0"), x) for x in xrange(1, 13)]
    EXP_YEAR = [(x, x) for x in xrange(date.today().year,
                                       date.today().year + 15)]

    def __init__(self, *args, **kwargs):
        fields = (
            forms.ChoiceField(choices=self.EXP_MONTH,
                widget=StripeSelect('exp-month')),
            forms.ChoiceField(choices=self.EXP_YEAR,
                widget=StripeSelect('exp-year')),
        )
        super(CCExpField, self).__init__(fields, *args, **kwargs)
        self.widget = CCExpWidget(widgets =
            [fields[0].widget, fields[1].widget])


class StripePaymentForm(forms.Form):
    number = forms.IntegerField(label="Card Number",
        widget=StripeTextInput('number'))
    name = forms.CharField(label="Card Holder Name", max_length=60,
        widget=StripeTextInput('name'))
    expiration = CCExpField(label="Expiration")
    cvc = forms.CharField(label="CVC Number", max_length=4,
        widget=StripeTextInput('cvs'))


class StripeProcessPayment:
    def __init__(self, payment_data):
        self.last_error = ''
        self.payment_data = payment_data

    def is_valid(self):
        result = self.process_payment()
        if not result:
            email = conf_settings.DJANGO_CONFERENCE_CONTACT_EMAIL
            error = u"""
                We encountered an error while processing your credit card.
                Please contact <a href="mailto:%s">%s</a> for assistance.
            """
            self.last_error = mark_safe(error % (email, email))
            return False
        elif result != 'success':
            self.last_error = u'We encountered the following error while '+\
                u'processing your credit card: '+result
            return False
        return True

    def process_payment(self):
        if conf_settings.DJANGO_CONFERENCE_DISABLE_PAYMENT_PROCESSING:
            return 'success'
        if not self.payment_data or 'stripeToken' not in self.payment_data:
            return False
        stripe.api_key = conf_settings.DJANGO_CONFERENCE_STRIPE_SECRET_KEY
        two_places = Decimal('0.01')
        total_rounded = self.payment_data['total'].quantize(two_places)
        total_cents = str(total_rounded).replace('.', '')
        # Create the charge on Stripe's servers - this will charge the user's card
        try:
            charge = stripe.Charge.create(
                currency="usd",
                amount=total_cents,
                card=self.payment_data['stripeToken'],
                description=self.payment_data['description'],
            )
            return 'success'
        except stripe.CardError, e:
          # The card has been declined
            return unicode(e)
