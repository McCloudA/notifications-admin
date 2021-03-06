import weakref
from datetime import datetime, timedelta
from itertools import chain

import pytz
from flask import request
from flask_login import current_user
from flask_wtf import FlaskForm as Form
from flask_wtf.file import FileAllowed
from flask_wtf.file import FileField as FileField_wtf
from notifications_utils.columns import Columns
from notifications_utils.formatters import strip_whitespace
from notifications_utils.postal_address import PostalAddress
from notifications_utils.recipients import (
    InvalidPhoneError,
    normalise_phone_number,
    validate_phone_number,
)
from wtforms import (
    BooleanField,
    DateField,
    FieldList,
    FileField,
    HiddenField,
    IntegerField,
    PasswordField,
)
from wtforms import RadioField as WTFormsRadioField
from wtforms import (
    SelectMultipleField,
    StringField,
    TextAreaField,
    ValidationError,
    validators,
    widgets,
)
from wtforms.fields.html5 import EmailField, SearchField, TelField
from wtforms.validators import URL, DataRequired, Length, Optional, Regexp
from wtforms.widgets import CheckboxInput, ListWidget

from app import format_thousands
from app.main.validators import (
    Blacklist,
    CsvFileValidator,
    DoesNotStartWithDoubleZero,
    LettersNumbersFullStopsAndUnderscoresOnly,
    MustContainAlphanumericCharacters,
    NoCommasInPlaceHolders,
    NoEmbeddedImagesInSVG,
    OnlySMSCharacters,
    ValidEmail,
    ValidGovEmail,
)
from app.models.feedback import PROBLEM_TICKET_TYPE, QUESTION_TICKET_TYPE
from app.models.organisation import Organisation
from app.models.roles_and_permissions import permissions, roles
from app.utils import guess_name_from_email_address


def get_time_value_and_label(future_time):
    return (
        future_time.replace(tzinfo=None).isoformat(),
        '{} at {}'.format(
            get_human_day(future_time.astimezone(pytz.timezone('Europe/London'))),
            get_human_time(future_time.astimezone(pytz.timezone('Europe/London')))
        )
    )


def get_human_time(time):
    return {
        '0': 'midnight',
        '12': 'midday'
    }.get(
        time.strftime('%-H'),
        time.strftime('%-I%p').lower()
    )


def get_human_day(time, prefix_today_with='T'):
    #  Add 1 hour to get ‘midnight today’ instead of ‘midnight tomorrow’
    time = (time - timedelta(hours=1)).strftime('%A')
    if time == datetime.utcnow().strftime('%A'):
        return '{}oday'.format(prefix_today_with)
    if time == (datetime.utcnow() + timedelta(days=1)).strftime('%A'):
        return 'Tomorrow'
    return time


def get_furthest_possible_scheduled_time():
    return (datetime.utcnow() + timedelta(days=4)).replace(hour=0)


def get_next_hours_until(until):
    now = datetime.utcnow()
    hours = int((until - now).total_seconds() / (60 * 60))
    return [
        (now + timedelta(hours=i)).replace(minute=0, second=0).replace(tzinfo=pytz.utc)
        for i in range(1, hours + 1)
    ]


def get_next_days_until(until):
    now = datetime.utcnow()
    days = int((until - now).total_seconds() / (60 * 60 * 24))
    return [
        get_human_day(
            (now + timedelta(days=i)).replace(tzinfo=pytz.utc),
            prefix_today_with='Later t'
        )
        for i in range(0, days + 1)
    ]


class MultiCheckboxField(SelectMultipleField):
    widget = ListWidget(prefix_label=False)
    option_widget = CheckboxInput()


class RadioField(WTFormsRadioField):

    def __init__(
        self,
        *args,
        thing='an option',
        **kwargs
    ):
        super().__init__(*args, **kwargs)
        self.thing = thing
        self.validate_choice = False

    def pre_validate(self, form):
        super().pre_validate(form)
        if self.data not in dict(self.choices).keys():
            raise ValidationError(f'Select {self.thing}')


def email_address(label='Email address', gov_user=True, required=True):

    validators = [
        ValidEmail(),
    ]

    if gov_user:
        validators.append(ValidGovEmail())

    if required:
        validators.append(DataRequired(message='Cannot be empty'))

    return EmailField(label, validators, render_kw={'spellcheck': 'false'})


class UKMobileNumber(TelField):
    def pre_validate(self, form):
        try:
            validate_phone_number(self.data)
        except InvalidPhoneError as e:
            raise ValidationError(str(e))


class InternationalPhoneNumber(TelField):
    def pre_validate(self, form):
        try:
            if self.data:
                validate_phone_number(self.data, international=True)
        except InvalidPhoneError as e:
            raise ValidationError(str(e))


def uk_mobile_number(label='Mobile number'):
    return UKMobileNumber(label,
                          validators=[DataRequired(message='Cannot be empty')])


def international_phone_number(label='Mobile number'):
    return InternationalPhoneNumber(
        label,
        validators=[DataRequired(message='Cannot be empty')]
    )


def password(label='Password'):
    return PasswordField(label,
                         validators=[DataRequired(message='Cannot be empty'),
                                     Length(8, 255, message='Must be at least 8 characters'),
                                     Blacklist(message='Choose a password that’s harder to guess')])


class SMSCode(StringField):
    validators = [
        DataRequired(message='Cannot be empty'),
        Regexp(regex=r'^\d+$', message='Numbers only'),
        Length(min=5, message='Not enough numbers'),
        Length(max=5, message='Too many numbers'),
    ]

    def __call__(self, **kwargs):
        return super().__call__(type='tel', pattern='[0-9]*', **kwargs)

    def process_formdata(self, valuelist):
        if valuelist:
            self.data = Columns.make_key(valuelist[0])


class ForgivingIntegerField(StringField):

    #  Actual value is 2147483647 but this is a scary looking arbitrary number
    POSTGRES_MAX_INT = 2000000000

    def __init__(
        self,
        label=None,
        things='items',
        format_error_suffix='',
        **kwargs
    ):
        self.things = things
        self.format_error_suffix = format_error_suffix
        super().__init__(label, **kwargs)

    def process_formdata(self, valuelist):

        if valuelist:

            value = valuelist[0].replace(',', '').replace(' ', '')

            try:
                value = int(value)
            except ValueError:
                pass

            if value == '':
                value = 0

        return super().process_formdata([value])

    def pre_validate(self, form):

        if self.data:
            error = None
            try:
                if int(self.data) > self.POSTGRES_MAX_INT:
                    error = 'Number of {} must be {} or less'.format(
                        self.things,
                        format_thousands(self.POSTGRES_MAX_INT),
                    )
            except ValueError:
                error = 'Enter the number of {} {}'.format(
                    self.things,
                    self.format_error_suffix,
                )

            if error:
                raise ValidationError(error)

        return super().pre_validate(form)

    def __call__(self, **kwargs):

        if self.get_form().is_submitted() and not self.get_form().validate():
            return super().__call__(
                value=(self.raw_data or [None])[0],
                **kwargs
            )

        try:
            value = int(self.data)
            value = format_thousands(value)
        except (ValueError, TypeError):
            value = self.data if self.data is not None else ''

        return super().__call__(value=value, **kwargs)


class OrganisationTypeField(RadioField):
    def __init__(
        self,
        *args,
        include_only=None,
        validators=None,
        **kwargs
    ):
        super().__init__(
            *args,
            choices=[
                (value, label) for value, label in Organisation.TYPES
                if not include_only or value in include_only
            ],
            thing='the type of organisation',
            validators=validators or [],
            **kwargs
        )


class FieldWithNoneOption():

    # This is a special value that is specific to our forms. This is
    # more expicit than casting `None` to a string `'None'` which can
    # have unexpected edge cases
    NONE_OPTION_VALUE = '__NONE__'

    # When receiving Python data, eg when instantiating the form object
    # we want to convert that data to our special value, so that it gets
    # recognised as being one of the valid choices
    def process_data(self, value):
        self.data = self.NONE_OPTION_VALUE if value is None else value

    # After validation we want to convert it back to a Python `None` for
    # use elsewhere, eg posting to the API
    def post_validate(self, form, validation_stopped):
        if self.data == self.NONE_OPTION_VALUE and not validation_stopped:
            self.data = None


class RadioFieldWithNoneOption(FieldWithNoneOption, RadioField):
    pass


class NestedFieldMixin:
    def children(self):
        # start map with root option as a single child entry
        child_map = {None: [option for option in self
                            if option.data == self.NONE_OPTION_VALUE]}

        # add entries for all other children
        for option in self:
            if option.data == self.NONE_OPTION_VALUE:
                child_ids = [
                    folder['id'] for folder in self.all_template_folders
                    if folder['parent_id'] is None]
                key = self.NONE_OPTION_VALUE
            else:
                child_ids = [
                    folder['id'] for folder in self.all_template_folders
                    if folder['parent_id'] == option.data]
                key = option.data

            child_map[key] = [option for option in self if option.data in child_ids]

        return child_map


class NestedRadioField(RadioFieldWithNoneOption, NestedFieldMixin):
    pass


class NestedCheckboxesField(SelectMultipleField, NestedFieldMixin):
    NONE_OPTION_VALUE = None


class HiddenFieldWithNoneOption(FieldWithNoneOption, HiddenField):
    pass


class RadioFieldWithRequiredMessage(RadioField):
    def __init__(self, *args, required_message='Not a valid choice', **kwargs):
        self.required_message = required_message
        super().__init__(*args, **kwargs)

    def pre_validate(self, form):
        try:
            return super().pre_validate(form)
        except ValueError:
            raise ValueError(self.required_message)


class StripWhitespaceForm(Form):
    class Meta:
        def bind_field(self, form, unbound_field, options):
            # FieldList simply doesn't support filters.
            # @see: https://github.com/wtforms/wtforms/issues/148
            no_filter_fields = (FieldList, PasswordField)
            filters = [strip_whitespace] if not issubclass(unbound_field.field_class, no_filter_fields) else []
            filters += unbound_field.kwargs.get('filters', [])
            bound = unbound_field.bind(form=form, filters=filters, **options)
            bound.get_form = weakref.ref(form)  # GC won't collect the form if we don't use a weakref
            return bound


class StripWhitespaceStringField(StringField):
    def __init__(self, label=None, **kwargs):
        kwargs['filters'] = tuple(chain(
            kwargs.get('filters', ()),
            (
                strip_whitespace,
            ),
        ))
        super(StringField, self).__init__(label, **kwargs)


class PostalAddressField(TextAreaField):
    def process_formdata(self, valuelist):
        if valuelist:
            self.data = PostalAddress(valuelist[0]).normalised


class OnOffField(RadioField):

    def __init__(self, label, choices=None, *args, **kwargs):
        choices = choices or [
            (True, 'On'),
            (False, 'Off'),
        ]
        super().__init__(
            label,
            choices=choices,
            thing=f'{choices[0][1].lower()} or {choices[1][1].lower()}',
            *args,
            **kwargs,
        )

    def process_formdata(self, valuelist):
        if valuelist:
            value = valuelist[0]
            self.data = (value == 'True') if value in ['True', 'False'] else value

    def iter_choices(self):
        for value, label in self.choices:
            # This overrides WTForms default behaviour which is to check
            # self.coerce(value) == self.data
            # where self.coerce returns a string for a boolean input
            yield (
                value,
                label,
                (self.data in {value, self.coerce(value)})
            )


class LoginForm(StripWhitespaceForm):
    email_address = EmailField('Email address', validators=[
        Length(min=5, max=255),
        DataRequired(message='Cannot be empty'),
        ValidEmail()
    ])
    password = PasswordField('Password', validators=[
        DataRequired(message='Enter your password')
    ])


class RegisterUserForm(StripWhitespaceForm):
    name = StringField('Full name',
                       validators=[DataRequired(message='Cannot be empty')])
    email_address = email_address()
    mobile_number = international_phone_number()
    password = password()
    # always register as sms type
    auth_type = HiddenField('auth_type', default='sms_auth')


class RegisterUserFromInviteForm(RegisterUserForm):
    def __init__(self, invited_user):
        super().__init__(
            service=invited_user.service,
            email_address=invited_user.email_address,
            auth_type=invited_user.auth_type,
            name=guess_name_from_email_address(
                invited_user.email_address
            ),
        )

    mobile_number = InternationalPhoneNumber('Mobile number', validators=[])
    service = HiddenField('service')
    email_address = HiddenField('email_address')
    auth_type = HiddenField('auth_type', validators=[DataRequired()])

    def validate_mobile_number(self, field):
        if self.auth_type.data == 'sms_auth' and not field.data:
            raise ValidationError('Cannot be empty')


class RegisterUserFromOrgInviteForm(StripWhitespaceForm):
    def __init__(self, invited_org_user):
        super().__init__(
            organisation=invited_org_user.organisation,
            email_address=invited_org_user.email_address,
        )

    name = StringField(
        'Full name',
        validators=[DataRequired(message='Cannot be empty')]
    )

    mobile_number = InternationalPhoneNumber('Mobile number', validators=[DataRequired(message='Cannot be empty')])
    password = password()
    organisation = HiddenField('organisation')
    email_address = HiddenField('email_address')
    auth_type = HiddenField('auth_type', validators=[DataRequired()])


PermissionsAbstract = type("PermissionsAbstract", (StripWhitespaceForm,), {
    permission: BooleanField(label) for permission, label in permissions
})


class PermissionsForm(PermissionsAbstract):
    def __init__(self, all_template_folders=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.folder_permissions.choices = []
        if all_template_folders is not None:
            self.folder_permissions.all_template_folders = all_template_folders
            self.folder_permissions.choices = [
                (item['id'], item['name']) for item in ([{'name': 'Templates', 'id': None}] + all_template_folders)
            ]

    folder_permissions = NestedCheckboxesField('Folders this team member can see')

    login_authentication = RadioField(
        'Sign in using',
        choices=[
            ('sms_auth', 'Text message code'),
            ('email_auth', 'Email link'),
        ],
        thing='how this team member should sign in',
        validators=[DataRequired()]
    )

    @property
    def permissions(self):
        return {role for role in roles.keys() if self[role].data is True}

    @property
    def permissions_fields(self):
        return (getattr(self, permission) for permission, _ in permissions)

    @classmethod
    def from_user(cls, user, service_id, **kwargs):
        return cls(
            **kwargs,
            **{
                role: user.has_permission_for_service(service_id, role)
                for role in roles.keys()
            },
            login_authentication=user.auth_type
        )


class InviteUserForm(PermissionsForm):
    email_address = email_address(gov_user=False)

    def __init__(self, invalid_email_address, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.invalid_email_address = invalid_email_address.lower()

    def validate_email_address(self, field):
        if field.data.lower() == self.invalid_email_address and not current_user.platform_admin:
            raise ValidationError("You cannot send an invitation to yourself")


class InviteOrgUserForm(StripWhitespaceForm):
    email_address = email_address(gov_user=False)

    def __init__(self, invalid_email_address, *args, **kwargs):
        super(InviteOrgUserForm, self).__init__(*args, **kwargs)
        self.invalid_email_address = invalid_email_address.lower()

    def validate_email_address(self, field):
        if field.data.lower() == self.invalid_email_address and not current_user.platform_admin:
            raise ValidationError("You cannot send an invitation to yourself")


class TwoFactorForm(StripWhitespaceForm):
    def __init__(self, validate_code_func, *args, **kwargs):
        '''
        Keyword arguments:
        validate_code_func -- Validates the code with the API.
        '''
        self.validate_code_func = validate_code_func
        super(TwoFactorForm, self).__init__(*args, **kwargs)

    sms_code = SMSCode('Text message code')

    def validate(self):

        if not self.sms_code.validate(self):
            return False

        is_valid, reason = self.validate_code_func(self.sms_code.data)

        if not is_valid:
            self.sms_code.errors.append(reason)
            return False

        return True


class TextNotReceivedForm(StripWhitespaceForm):
    mobile_number = international_phone_number()


class RenameServiceForm(StripWhitespaceForm):
    name = StringField(
        u'Service name',
        validators=[
            DataRequired(message='Cannot be empty'),
            MustContainAlphanumericCharacters()
        ])


class RenameOrganisationForm(StripWhitespaceForm):
    name = StringField(
        u'Organisation name',
        validators=[
            DataRequired(message='Cannot be empty'),
            MustContainAlphanumericCharacters()
        ])


class AddGPOrganisationForm(StripWhitespaceForm):

    def __init__(self, *args, service_name='unknown', **kwargs):
        super().__init__(*args, **kwargs)
        self.same_as_service_name.label.text = 'Is your GP practice called ‘{}’?'.format(service_name)
        self.service_name = service_name

    def get_organisation_name(self):
        if self.same_as_service_name.data:
            return self.service_name
        return self.name.data

    same_as_service_name = OnOffField(
        'Is your GP practice called the same name as your service?',
        choices=(
            (True, 'Yes'),
            (False, 'No'),
        ),
    )

    name = StringField(
        'What’s your practice called?',
    )

    def validate_name(self, field):
        if self.same_as_service_name.data is False:
            if not field.data:
                raise ValidationError('Cannot be empty')
        else:
            field.data = ''


class AddNHSLocalOrganisationForm(StripWhitespaceForm):

    def __init__(self, *args, organisation_choices=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.organisations.choices = organisation_choices

    organisations = RadioField(
        'Which NHS Trust or Clinical Commissioning Group do you work for?',
        thing='an NHS Trust or Clinical Commissioning Group'
    )


class OrganisationOrganisationTypeForm(StripWhitespaceForm):
    organisation_type = OrganisationTypeField('What type of organisation is this?')


class OrganisationCrownStatusForm(StripWhitespaceForm):
    crown_status = RadioField(
        (
            'Is this organisation a crown body?'
        ),
        choices=[
            ('crown', 'Yes'),
            ('non-crown', 'No'),
            ('unknown', 'Not sure'),
        ],
        thing='whether this organisation is a crown body',
    )


class OrganisationAgreementSignedForm(StripWhitespaceForm):
    agreement_signed = RadioField(
        (
            'Has this organisation signed the agreement?'
        ),
        choices=[
            ('yes', 'Yes'),
            ('no', 'No'),
            ('unknown', 'No (but we have some service-specific agreements in place)'),
        ],
        thing='whether this organisation has signed the agreement',
    )


class OrganisationDomainsForm(StripWhitespaceForm):

    def populate(self, domains_list):
        for index, value in enumerate(domains_list):
            self.domains[index].data = value

    domains = FieldList(
        StripWhitespaceStringField(
            '',
            validators=[
                Optional(),
            ],
            default=''
        ),
        min_entries=20,
        max_entries=20,
        label="Domain names"
    )


class CreateServiceForm(StripWhitespaceForm):
    name = StringField(
        "What’s your service called?",
        validators=[
            DataRequired(message='Cannot be empty'),
            MustContainAlphanumericCharacters()
        ])
    organisation_type = OrganisationTypeField('Who runs this service?')


class CreateNhsServiceForm(CreateServiceForm):
    organisation_type = OrganisationTypeField(
        'Who runs this service?',
        include_only={'nhs_central', 'nhs_local', 'nhs_gp'},
    )


class NewOrganisationForm(
    RenameOrganisationForm,
    OrganisationOrganisationTypeForm,
    OrganisationCrownStatusForm,
):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Don’t offer the ‘not sure’ choice
        self.crown_status.choices = self.crown_status.choices[:-1]


class FreeSMSAllowance(StripWhitespaceForm):
    free_sms_allowance = IntegerField(
        'Numbers of text message fragments per year',
        validators=[
            DataRequired(message='Cannot be empty')
        ]
    )


class ConfirmPasswordForm(StripWhitespaceForm):
    def __init__(self, validate_password_func, *args, **kwargs):
        self.validate_password_func = validate_password_func
        super(ConfirmPasswordForm, self).__init__(*args, **kwargs)

    password = PasswordField(u'Enter password')

    def validate_password(self, field):
        if not self.validate_password_func(field.data):
            raise ValidationError('Invalid password')


class BaseTemplateForm(StripWhitespaceForm):
    name = StringField(
        u'Template name',
        validators=[DataRequired(message="Cannot be empty")])

    template_content = TextAreaField(
        u'Message',
        validators=[
            DataRequired(message="Cannot be empty"),
            NoCommasInPlaceHolders()
        ]
    )
    process_type = RadioField(
        'Use priority queue?',
        choices=[
            ('priority', 'Yes'),
            ('normal', 'No'),
        ],
        thing='yes or no',
        validators=[DataRequired()],
        default='normal'
    )


class SMSTemplateForm(BaseTemplateForm):
    def validate_template_content(self, field):
        OnlySMSCharacters()(None, field)


class LetterAddressForm(StripWhitespaceForm):

    def __init__(self, *args, allow_international_letters=False, **kwargs):
        self.allow_international_letters = allow_international_letters
        super().__init__(*args, **kwargs)

    address = PostalAddressField(
        'Address',
        validators=[DataRequired(message="Cannot be empty")]
    )

    def validate_address(self, field):

        address = PostalAddress(
            field.data,
            allow_international_letters=self.allow_international_letters,
        )

        if not address.has_enough_lines:
            raise ValidationError(
                f'Address must be at least {PostalAddress.MIN_LINES} lines long'
            )

        if address.has_too_many_lines:
            raise ValidationError(
                f'Address must be no more than {PostalAddress.MAX_LINES} lines long'
            )

        if not address.has_valid_last_line:
            if self.allow_international_letters:
                raise ValidationError(
                    f'Last line of the address must be a UK postcode or another country'
                )
            raise ValidationError(
                f'Last line of the address must be a real UK postcode'
            )


class EmailTemplateForm(BaseTemplateForm):
    subject = TextAreaField(
        u'Subject',
        validators=[DataRequired(message="Cannot be empty")])


class LetterTemplateForm(EmailTemplateForm):
    subject = TextAreaField(
        u'Main heading',
        validators=[DataRequired(message="Cannot be empty")])

    template_content = TextAreaField(
        u'Body',
        validators=[
            DataRequired(message="Cannot be empty"),
            NoCommasInPlaceHolders()
        ]
    )


class LetterTemplatePostageForm(StripWhitespaceForm):
    postage = RadioField(
        'Choose the postage for this letter template',
        choices=[
            ('first', 'First class'),
            ('second', 'Second class'),
        ],
        thing='first class or second class',
        validators=[DataRequired()]
    )


class LetterUploadPostageForm(StripWhitespaceForm):
    postage = RadioField(
        'Choose the postage for this letter',
        choices=[
            ('first', 'First class post'),
            ('second', 'Second class post'),
        ],
        default='second',
        validators=[DataRequired()]
    )
    file_id = HiddenField(
        validators=[DataRequired()]
    )


class ForgotPasswordForm(StripWhitespaceForm):
    email_address = email_address(gov_user=False)


class NewPasswordForm(StripWhitespaceForm):
    new_password = password()


class ChangePasswordForm(StripWhitespaceForm):
    def __init__(self, validate_password_func, *args, **kwargs):
        self.validate_password_func = validate_password_func
        super(ChangePasswordForm, self).__init__(*args, **kwargs)

    old_password = password('Current password')
    new_password = password('New password')

    def validate_old_password(self, field):
        if not self.validate_password_func(field.data):
            raise ValidationError('Invalid password')


class CsvUploadForm(StripWhitespaceForm):
    file = FileField('Add recipients', validators=[DataRequired(
        message='Please pick a file'), CsvFileValidator()])


class ChangeNameForm(StripWhitespaceForm):
    new_name = StringField(u'Your name')


class ChangeEmailForm(StripWhitespaceForm):
    def __init__(self, validate_email_func, *args, **kwargs):
        self.validate_email_func = validate_email_func
        super(ChangeEmailForm, self).__init__(*args, **kwargs)

    email_address = email_address()

    def validate_email_address(self, field):
        is_valid = self.validate_email_func(field.data)
        if is_valid:
            raise ValidationError("The email address is already in use")


class ChangeNonGovEmailForm(ChangeEmailForm):
    email_address = email_address(gov_user=False)


class ChangeMobileNumberForm(StripWhitespaceForm):
    mobile_number = international_phone_number()


class ChooseTimeForm(StripWhitespaceForm):

    def __init__(self, *args, **kwargs):
        super(ChooseTimeForm, self).__init__(*args, **kwargs)
        self.scheduled_for.choices = [('', 'Now')] + [
            get_time_value_and_label(hour) for hour in get_next_hours_until(
                get_furthest_possible_scheduled_time()
            )
        ]
        self.scheduled_for.categories = get_next_days_until(get_furthest_possible_scheduled_time())

    scheduled_for = RadioField(
        'When should Notify send these messages?',
        default='',
    )


class CreateKeyForm(StripWhitespaceForm):
    def __init__(self, existing_keys, *args, **kwargs):
        self.existing_key_names = [
            key['name'].lower() for key in existing_keys
            if not key['expiry_date']
        ]
        super().__init__(*args, **kwargs)

    key_type = RadioField(
        'Type of key',
        thing='the type of key',
    )

    key_name = StringField(u'Description of key', validators=[
        DataRequired(message='You need to give the key a name')
    ])

    def validate_key_name(self, key_name):
        if key_name.data.lower() in self.existing_key_names:
            raise ValidationError('A key with this name already exists')


class SupportType(StripWhitespaceForm):
    support_type = RadioField(
        'How can we help you?',
        choices=[
            (PROBLEM_TICKET_TYPE, 'Report a problem'),
            (QUESTION_TICKET_TYPE, 'Ask a question or give feedback'),
        ],
    )


class SupportRedirect(StripWhitespaceForm):
    who = RadioField(
        'What do you need help with?',
        choices=[
            ('public-sector', 'I work in the public sector and need to send emails, text messages or letters'),
            ('public', 'I’m a member of the public with a question for the government'),
        ],
    )


class FeedbackOrProblem(StripWhitespaceForm):
    name = StringField('Name (optional)')
    email_address = email_address(label='Email address', gov_user=False, required=True)
    feedback = TextAreaField('Your message', validators=[DataRequired(message="Cannot be empty")])


class Triage(StripWhitespaceForm):
    severe = RadioField(
        'Is it an emergency?',
        choices=[
            ('yes', 'Yes'),
            ('no', 'No'),
        ],
        thing='yes or no',
    )


class EstimateUsageForm(StripWhitespaceForm):

    volume_email = ForgivingIntegerField(
        'How many emails do you expect to send in the next year?',
        things='emails',
        format_error_suffix='you expect to send',
    )
    volume_sms = ForgivingIntegerField(
        'How many text messages do you expect to send in the next year?',
        things='text messages',
        format_error_suffix='you expect to send',
    )
    volume_letter = ForgivingIntegerField(
        'How many letters do you expect to send in the next year?',
        things='letters',
        format_error_suffix='you expect to send',
    )
    consent_to_research = RadioField(
        'Can we contact you when we’re doing user research?',
        choices=[
            ('yes', 'Yes'),
            ('no', 'No'),
        ],
        thing='yes or no',
    )

    at_least_one_volume_filled = True

    def validate(self, *args, **kwargs):

        if self.volume_email.data == self.volume_sms.data == self.volume_letter.data == 0:
            self.at_least_one_volume_filled = False
            return False

        return super().validate(*args, **kwargs)


class ProviderForm(StripWhitespaceForm):
    priority = IntegerField('Priority', [validators.NumberRange(min=1, max=100, message="Must be between 1 and 100")])


class ProviderRatioForm(StripWhitespaceForm):

    ratio = RadioField(choices=[
        (str(value), '{}% / {}%'.format(value, 100 - value))
        for value in range(100, -10, -10)
    ])

    @property
    def percentage_left(self):
        return int(self.ratio.data)

    @property
    def percentage_right(self):
        return 100 - self.percentage_left


class ServiceContactDetailsForm(StripWhitespaceForm):
    contact_details_type = RadioField(
        'Type of contact details',
        choices=[
            ('url', 'Link'),
            ('email_address', 'Email address'),
            ('phone_number', 'Phone number'),
        ],
    )

    url = StringField("URL")
    email_address = EmailField("Email address")
    phone_number = StringField("Phone number")

    def validate(self):

        if self.contact_details_type.data == 'url':
            self.url.validators = [DataRequired(), URL(message='Must be a valid URL')]

        elif self.contact_details_type.data == 'email_address':
            self.email_address.validators = [DataRequired(), Length(min=5, max=255), ValidEmail()]

        elif self.contact_details_type.data == 'phone_number':
            # we can't use the existing phone number validation functions here since we want to allow landlines
            def valid_phone_number(self, num):
                try:
                    normalise_phone_number(num.data)
                    return True
                except InvalidPhoneError:
                    raise ValidationError('Must be a valid phone number')
            self.phone_number.validators = [DataRequired(), Length(min=5, max=20), valid_phone_number]

        return super().validate()


class ServiceReplyToEmailForm(StripWhitespaceForm):
    email_address = email_address(label='Reply-to email address', gov_user=False)
    is_default = BooleanField("Make this email address the default")


class ServiceSmsSenderForm(StripWhitespaceForm):
    sms_sender = StringField(
        'Text message sender',
        validators=[
            DataRequired(message="Cannot be empty"),
            Length(max=11, message="Enter 11 characters or fewer"),
            Length(min=3, message="Enter 3 characters or more"),
            LettersNumbersFullStopsAndUnderscoresOnly(),
            DoesNotStartWithDoubleZero(),
        ]
    )
    is_default = BooleanField("Make this text message sender the default")


class ServiceEditInboundNumberForm(StripWhitespaceForm):
    is_default = BooleanField("Make this text message sender the default")


class ServiceLetterContactBlockForm(StripWhitespaceForm):
    letter_contact_block = TextAreaField(
        validators=[
            DataRequired(message="Cannot be empty"),
            NoCommasInPlaceHolders()
        ]
    )
    is_default = BooleanField("Set as your default address")

    def validate_letter_contact_block(self, field):
        line_count = field.data.strip().count('\n')
        if line_count >= 10:
            raise ValidationError(
                'Contains {} lines, maximum is 10'.format(line_count + 1)
            )


class ServiceOnOffSettingForm(StripWhitespaceForm):

    def __init__(self, name, *args, truthy='On', falsey='Off', **kwargs):
        super().__init__(*args, **kwargs)
        self.enabled.label.text = name
        self.enabled.choices = [
            (True, truthy),
            (False, falsey),
        ]

    enabled = OnOffField('Choices')


class ServiceSwitchChannelForm(ServiceOnOffSettingForm):
    def __init__(self, channel, *args, **kwargs):
        name = 'Send {}'.format({
            'email': 'emails',
            'sms': 'text messages',
            'letter': 'letters',
        }.get(channel))

        super().__init__(name, *args, **kwargs)


class SetEmailBranding(StripWhitespaceForm):

    branding_style = RadioFieldWithNoneOption(
        'Branding style',
        thing='a branding style',
    )

    DEFAULT = (FieldWithNoneOption.NONE_OPTION_VALUE, 'GOV.UK')

    def __init__(self, all_branding_options, current_branding):

        super().__init__(branding_style=current_branding)

        self.branding_style.choices = sorted(
            all_branding_options + [self.DEFAULT],
            key=lambda branding: (
                branding[0] != current_branding,
                branding[0] is not self.DEFAULT[0],
                branding[1].lower(),
            ),
        )


class SetLetterBranding(SetEmailBranding):
    # form is the same, but instead of GOV.UK we have None as a valid option
    DEFAULT = (FieldWithNoneOption.NONE_OPTION_VALUE, 'None')


class PreviewBranding(StripWhitespaceForm):

    branding_style = HiddenFieldWithNoneOption('branding_style')


class ServiceUpdateEmailBranding(StripWhitespaceForm):
    name = StringField('Name of brand')
    text = StringField('Text')
    colour = StringField(
        'Colour',
        validators=[
            Regexp(regex="^$|^#(?:[0-9a-fA-F]{3}){1,2}$", message='Must be a valid color hex code (starting with #)')
        ]
    )
    file = FileField_wtf('Upload a PNG logo', validators=[FileAllowed(['png'], 'PNG Images only!')])
    brand_type = RadioField(
        "Brand type",
        choices=[
            ('both', 'GOV.UK and branding'),
            ('org', 'Branding only'),
            ('org_banner', 'Branding banner'),
        ]
    )

    def validate_name(self, name):
        op = request.form.get('operation')
        if op == 'email-branding-details' and not self.name.data:
            raise ValidationError('This field is required')


class SVGFileUpload(StripWhitespaceForm):
    file = FileField_wtf(
        'Upload an SVG logo',
        validators=[
            FileAllowed(['svg'], 'SVG Images only!'),
            DataRequired(message="You need to upload a file to submit"),
            NoEmbeddedImagesInSVG()
        ]
    )


class ServiceLetterBrandingDetails(StripWhitespaceForm):
    name = StringField('Name of brand', validators=[DataRequired()])


class PDFUploadForm(StripWhitespaceForm):
    file = FileField_wtf(
        'Upload a letter in PDF format',
        validators=[
            FileAllowed(['pdf'], 'Save your letter as a PDF and try again.'),
            DataRequired(message="You need to choose a file to upload")
        ]
    )


class EmailFieldInWhitelist(EmailField, StripWhitespaceStringField):
    pass


class InternationalPhoneNumberInWhitelist(InternationalPhoneNumber, StripWhitespaceStringField):
    pass


class Whitelist(StripWhitespaceForm):

    def populate(self, email_addresses, phone_numbers):
        for form_field, existing_whitelist in (
            (self.email_addresses, email_addresses),
            (self.phone_numbers, phone_numbers)
        ):
            for index, value in enumerate(existing_whitelist):
                form_field[index].data = value

    email_addresses = FieldList(
        EmailFieldInWhitelist(
            '',
            validators=[
                Optional(),
                ValidEmail()
            ],
            default=''
        ),
        min_entries=5,
        max_entries=5,
        label="Email addresses"
    )

    phone_numbers = FieldList(
        InternationalPhoneNumberInWhitelist(
            '',
            validators=[
                Optional()
            ],
            default=''
        ),
        min_entries=5,
        max_entries=5,
        label="Mobile numbers"
    )


class DateFilterForm(StripWhitespaceForm):
    start_date = DateField("Start Date", [validators.optional()])
    end_date = DateField("End Date", [validators.optional()])
    include_from_test_key = BooleanField("Include test keys", default="checked", false_values={"N"})


class RequiredDateFilterForm(StripWhitespaceForm):
    start_date = DateField("Start Date")
    end_date = DateField("End Date")


class SearchByNameForm(StripWhitespaceForm):

    search = SearchField(
        'Search by name',
        validators=[DataRequired("You need to enter full or partial name to search by.")],
    )


class SearchUsersByEmailForm(StripWhitespaceForm):

    search = SearchField(
        'Search by name or email address',
        validators=[
            DataRequired("You need to enter full or partial email address to search by.")
        ],
    )


class SearchUsersForm(StripWhitespaceForm):

    search = SearchField('Search by name or email address')


class SearchNotificationsForm(StripWhitespaceForm):

    to = SearchField()

    labels = {
        'email': 'Search by email address',
        'sms': 'Search by phone number',
    }

    def __init__(self, message_type, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.to.label.text = self.labels.get(
            message_type,
            'Search by phone number or email address',
        )


class PlaceholderForm(StripWhitespaceForm):

    pass


class PasswordFieldShowHasContent(StringField):
    widget = widgets.PasswordInput(hide_value=False)


class ServiceInboundNumberForm(StripWhitespaceForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.inbound_number.choices = kwargs['inbound_number_choices']

    inbound_number = RadioField(
        "Select your inbound number",
        thing='an inbound number',
    )


class CallbackForm(StripWhitespaceForm):

    def validate(self):
        return super().validate() or self.url.data == ''


class ServiceReceiveMessagesCallbackForm(CallbackForm):
    url = StringField(
        "URL",
        validators=[DataRequired(message='Cannot be empty'),
                    Regexp(regex="^https.*", message='Must be a valid https URL')]
    )
    bearer_token = PasswordFieldShowHasContent(
        "Bearer token",
        validators=[DataRequired(message='Cannot be empty'),
                    Length(min=10, message='Must be at least 10 characters')]
    )


class ServiceDeliveryStatusCallbackForm(CallbackForm):
    url = StringField(
        "URL",
        validators=[DataRequired(message='Cannot be empty'),
                    Regexp(regex="^https.*", message='Must be a valid https URL')]
    )
    bearer_token = PasswordFieldShowHasContent(
        "Bearer token",
        validators=[DataRequired(message='Cannot be empty'),
                    Length(min=10, message='Must be at least 10 characters')]
    )


class InternationalSMSForm(StripWhitespaceForm):
    enabled = RadioField(
        'Send text messages to international phone numbers',
        choices=[
            ('on', 'On'),
            ('off', 'Off'),
        ],
    )


class SMSPrefixForm(StripWhitespaceForm):
    enabled = RadioField(
        '',
        choices=[
            ('on', 'On'),
            ('off', 'Off'),
        ],
    )


def get_placeholder_form_instance(
    placeholder_name,
    dict_to_populate_from,
    template_type,
    allow_international_phone_numbers=False,
):

    if (
        Columns.make_key(placeholder_name) == 'emailaddress' and
        template_type == 'email'
    ):
        field = email_address(label=placeholder_name, gov_user=False)
    elif (
        Columns.make_key(placeholder_name) == 'phonenumber' and
        template_type == 'sms'
    ):
        if allow_international_phone_numbers:
            field = international_phone_number(label=placeholder_name)
        else:
            field = uk_mobile_number(label=placeholder_name)
    else:
        field = StringField(placeholder_name, validators=[
            DataRequired(message='Cannot be empty')
        ])

    PlaceholderForm.placeholder_value = field

    return PlaceholderForm(
        placeholder_value=dict_to_populate_from.get(placeholder_name, '')
    )


class SetSenderForm(StripWhitespaceForm):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.sender.choices = kwargs['sender_choices']
        self.sender.label.text = kwargs['sender_label']

    sender = RadioField()


class SetTemplateSenderForm(StripWhitespaceForm):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.sender.choices = kwargs['sender_choices']
        self.sender.label.text = 'Select your sender'

    sender = RadioField()


class LinkOrganisationsForm(StripWhitespaceForm):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.organisations.choices = kwargs['choices']

    organisations = RadioField(
        'Select an organisation',
        validators=[
            DataRequired()
        ]
    )


class BrandingOptions(StripWhitespaceForm):

    FALLBACK_OPTION_VALUE = 'something_else'
    FALLBACK_OPTION = (FALLBACK_OPTION_VALUE, 'Something else')

    options = RadioField('Choose your new branding')
    something_else = TextAreaField('Describe the branding you want')

    def __init__(self, service, *args, branding_type="email", **kwargs):
        super().__init__(*args, **kwargs)
        self.options.choices = tuple(self.get_available_choices(service, branding_type))
        self.options.label.text = 'Choose your new {} branding'.format(branding_type)
        if self.something_else_is_only_option:
            self.options.data = self.FALLBACK_OPTION_VALUE

    @staticmethod
    def get_available_choices(service, branding_type):
        if branding_type == "email":
            organisation_branding_id = service.organisation.email_branding_id if service.organisation else None
            service_branding_id = service.email_branding_id
            service_branding_name = service.email_branding_name
        elif branding_type == "letter":
            organisation_branding_id = service.organisation.letter_branding_id if service.organisation else None
            service_branding_id = service.letter_branding_id
            service_branding_name = service.letter_branding_name

        if (
            service.organisation_type == Organisation.TYPE_CENTRAL
            and organisation_branding_id is None
            and service_branding_id is not None
            and branding_type == "email"
        ):
            yield ('govuk', 'GOV.UK')

        if (
            service.organisation_type == Organisation.TYPE_CENTRAL
            and service.organisation
            and organisation_branding_id is None
            and service_branding_name.lower() != 'GOV.UK and {}'.format(service.organisation.name).lower()
            and branding_type == "email"
        ):
            yield ('govuk_and_org', 'GOV.UK and {}'.format(service.organisation.name))

        if (
            service.organisation_type in {
                Organisation.TYPE_NHS_CENTRAL,
                Organisation.TYPE_NHS_LOCAL,
                Organisation.TYPE_NHS_GP,
            }
            and service_branding_name != 'NHS'
        ):
            yield ('nhs', 'NHS')

        if (
            service.organisation
            and service.organisation_type not in {
                Organisation.TYPE_NHS_LOCAL,
                Organisation.TYPE_NHS_CENTRAL,
                Organisation.TYPE_NHS_GP,
            }
            and (
                service_branding_id is None
                or service_branding_id != organisation_branding_id
            )
        ):
            yield ('organisation', service.organisation.name)

        yield BrandingOptions.FALLBACK_OPTION

    @property
    def something_else_is_only_option(self):
        return self.options.choices == (self.FALLBACK_OPTION,)

    def validate_something_else(self, field):
        if (
            self.something_else_is_only_option
            or self.options.data == self.FALLBACK_OPTION_VALUE
        ) and not field.data:
            raise ValidationError('Cannot be empty')

        if self.options.data != self.FALLBACK_OPTION_VALUE:
            field.data = ''


class ServiceDataRetentionForm(StripWhitespaceForm):

    notification_type = RadioField(
        'What notification type?',
        choices=[
            ('email', 'Email'),
            ('sms', 'SMS'),
            ('letter', 'Letter'),
        ],
        validators=[DataRequired()],
    )
    days_of_retention = IntegerField(label="Days of retention",
                                     validators=[validators.NumberRange(min=3, max=90,
                                                                        message="Must be between 3 and 90")],
                                     )


class ServiceDataRetentionEditForm(StripWhitespaceForm):
    days_of_retention = IntegerField(label="Days of retention",
                                     validators=[validators.NumberRange(min=3, max=90,
                                                                        message="Must be between 3 and 90")],
                                     )


class ReturnedLettersForm(StripWhitespaceForm):
    references = TextAreaField(
        u'Letter references',
        validators=[
            DataRequired(message="Cannot be empty"),
        ]
    )


class TemplateFolderForm(StripWhitespaceForm):
    def __init__(self, all_service_users=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if all_service_users is not None:
            self.users_with_permission.all_service_users = all_service_users
            self.users_with_permission.choices = [
                (item.id, item.name) for item in all_service_users
            ]

    users_with_permission = MultiCheckboxField('Team members who can see this folder')
    name = StringField('Folder name', validators=[DataRequired(message='Cannot be empty')])


def required_for_ops(*operations):
    operations = set(operations)

    def validate(form, field):
        if form.op not in operations and any(field.raw_data):
            # super weird
            raise validators.StopValidation('Must be empty')
        if form.op in operations and not any(field.raw_data):
            raise validators.StopValidation('Cannot be empty')
    return validate


class TemplateAndFoldersSelectionForm(Form):
    """
    This form expects the form data to include an operation, based on which submit button is clicked.
    If enter is pressed, unknown will be sent by a hidden submit button at the top of the form.
    The value of this operation affects which fields are required, expected to be empty, or optional.

    * unknown
        currently not implemented, but in the future will try and work out if there are any obvious commands that can be
        assumed based on which fields are empty vs populated.
    * move-to-existing-folder
        must have data for templates_and_folders checkboxes, and move_to radios
    * move-to-new-folder
        must have data for move_to_new_folder_name, cannot have data for move_to_existing_folder_name
    * add-new-folder
        must have data for move_to_existing_folder_name, cannot have data for move_to_new_folder_name
    """

    ALL_TEMPLATES_FOLDER = {
        'name': 'Templates',
        'id': RadioFieldWithNoneOption.NONE_OPTION_VALUE,
    }

    def __init__(
        self,
        all_template_folders,
        template_list,
        allow_adding_letter_template,
        allow_adding_copy_of_template,
        *args,
        **kwargs
    ):

        super().__init__(*args, **kwargs)

        self.templates_and_folders.choices = template_list.as_id_and_name

        self.op = None
        self.is_move_op = self.is_add_folder_op = self.is_add_template_op = False

        self.move_to.all_template_folders = all_template_folders
        self.move_to.choices = [
            (item['id'], item['name'])
            for item in ([self.ALL_TEMPLATES_FOLDER] + all_template_folders)
        ]

        self.add_template_by_template_type.choices = list(filter(None, [
            ('email', 'Email'),
            ('sms', 'Text message'),
            ('letter', 'Letter') if allow_adding_letter_template else None,
            ('copy-existing', 'Copy an existing template') if allow_adding_copy_of_template else None,
        ]))

    def is_selected(self, template_folder_id):
        return template_folder_id in (self.templates_and_folders.data or [])

    def validate(self):
        self.op = request.form.get('operation')

        self.is_move_op = self.op in {'move-to-existing-folder', 'move-to-new-folder'}
        self.is_add_folder_op = self.op in {'add-new-folder', 'move-to-new-folder'}
        self.is_add_template_op = self.op in {'add-new-template'}

        if not (self.is_add_folder_op or self.is_move_op or self.is_add_template_op):
            return False

        return super().validate()

    def get_folder_name(self):
        if self.op == 'add-new-folder':
            return self.add_new_folder_name.data
        elif self.op == 'move-to-new-folder':
            return self.move_to_new_folder_name.data
        return None

    templates_and_folders = MultiCheckboxField('Choose templates or folders', validators=[
        required_for_ops('move-to-new-folder', 'move-to-existing-folder')
    ])
    # if no default set, it is set to None, which process_data transforms to '__NONE__'
    # this means '__NONE__' (self.ALL_TEMPLATES option) is selected when no form data has been submitted
    # set default to empty string so process_data method doesn't perform any transformation
    move_to = NestedRadioField(
        'Choose a folder',
        default='',
        validators=[
            required_for_ops('move-to-existing-folder'),
            Optional()
        ])
    add_new_folder_name = StringField('Folder name', validators=[required_for_ops('add-new-folder')])
    move_to_new_folder_name = StringField('Folder name', validators=[required_for_ops('move-to-new-folder')])

    add_template_by_template_type = RadioFieldWithRequiredMessage('New template', validators=[
        required_for_ops('add-new-template'),
        Optional(),
    ], required_message='Select the type of template you want to add')


class ClearCacheForm(StripWhitespaceForm):
    model_type = RadioField(
        'What do you want to clear today',
    )


class GoLiveNotesForm(StripWhitespaceForm):
    request_to_go_live_notes = TextAreaField(
        'Go live notes',
        filters=[lambda x: x or None],
    )


class AcceptAgreementForm(StripWhitespaceForm):

    @classmethod
    def from_organisation(cls, org):

        if org.agreement_signed_on_behalf_of_name and org.agreement_signed_on_behalf_of_email_address:
            who = 'someone-else'
        elif org.agreement_signed_version:  # only set if user has submitted form previously
            who = 'me'
        else:
            who = None

        return cls(
            version=org.agreement_signed_version,
            who=who,
            on_behalf_of_name=org.agreement_signed_on_behalf_of_name,
            on_behalf_of_email=org.agreement_signed_on_behalf_of_email_address,
        )

    version = StringField(
        'Which version of the agreement do you want to accept?'
    )

    who = RadioField(
        'Who are you accepting the agreement for?',
        choices=(
            (
                'me',
                'Yourself',
            ),
            (
                'someone-else',
                'Someone else',
            ),
        ),
    )

    on_behalf_of_name = StringField(
        'What’s their name?'
    )

    on_behalf_of_email = email_address(
        'What’s their email address?',
        required=False,
        gov_user=False,
    )

    def __validate_if_nominating(self, field):
        if self.who.data == 'someone-else':
            if not field.data:
                raise ValidationError('Cannot be empty')
        else:
            field.data = ''

    validate_on_behalf_of_name = __validate_if_nominating
    validate_on_behalf_of_email = __validate_if_nominating

    def validate_version(self, field):
        try:
            float(field.data)
        except (TypeError, ValueError):
            raise ValidationError("Must be a number")
