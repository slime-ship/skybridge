from django.contrib.auth.forms import UserCreationForm
from django import forms
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from .models import * 
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth import get_user_model
from .models import KYC, Loan
from django.core.validators import MinValueValidator
import datetime

User = get_user_model()

# --- ALL YOUR FORMS ---

class LoanForm(forms.ModelForm):
    amount = forms.DecimalField(
        widget=forms.NumberInput(attrs={'class': 'form-control', 'placeholder': 'Enter amount', 'min': '100', 'step': '100'}),
        min_value=100
    )
    duration = forms.IntegerField(
        widget=forms.NumberInput(attrs={'class': 'form-control', 'placeholder': 'Months (1-360)', 'min': '1', 'max': '360'}),
        min_value=1, max_value=360
    )
    annual_income = forms.DecimalField(
        required=True,
        widget=forms.NumberInput(attrs={'class': 'form-control', 'placeholder': 'Annual income', 'min': '0'}),
        min_value=0
    )
    requested_date = forms.DateField(
        widget=forms.DateInput(attrs={'class': 'form-control', 'type': 'date', 'min': datetime.date.today().isoformat()}),
        initial=datetime.date.today
    )
    collateral = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={'class': 'form-control', 'rows': 3, 'placeholder': 'Describe any collateral (property, vehicle, etc.)'})
    )
    notes = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={'class': 'form-control', 'rows': 2, 'placeholder': 'Additional information'})
    )

    class Meta:
        model = Loan
        fields = ['amount', 'loan_type', 'purpose', 'duration', 'employment_status', 'annual_income', 'repayment_frequency', 'collateral', 'requested_date', 'notes']
        widgets = {
            'loan_type': forms.Select(attrs={'class': 'form-control'}),
            'purpose': forms.Select(attrs={'class': 'form-control'}),
            'employment_status': forms.Select(attrs={'class': 'form-control'}),
            'repayment_frequency': forms.Select(attrs={'class': 'form-control'}),
        }
    
    def clean(self):
        cleaned_data = super().clean()
        amount = cleaned_data.get('amount')
        annual_income = cleaned_data.get('annual_income')
        if amount and annual_income and annual_income > 0:
            if amount > annual_income * 5:
                self.add_error('amount', 'Loan amount cannot exceed 5 times your annual income')
        return cleaned_data

class KYCForm(forms.ModelForm):
    class Meta:
        model = KYC
        fields = ['id_front', 'id_back', 'selfie']

class InvestmentForm(forms.ModelForm):
    investment_plan = forms.ModelChoiceField(
        queryset=InvestmentPlan.objects.filter(is_active=True),
        empty_label="Select Investment Plan",
        widget=forms.Select(attrs={'class': 'form-control'})
    )
    amount_invested = forms.DecimalField(
        max_digits=15, decimal_places=2, min_value=100.00,
        widget=forms.NumberInput(attrs={'placeholder': 'Enter amount to invest', 'class': 'form-control'})
    )

    class Meta:
        model = UserInvestment
        fields = ['investment_plan', 'amount_invested']

    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop('user', None)
        super().__init__(*args, **kwargs)
        self.fields['investment_plan'].queryset = InvestmentPlan.objects.filter(is_active=True)

    def clean_amount_invested(self):
        amount = self.cleaned_data['amount_invested']
        plan = self.cleaned_data.get('investment_plan')
        if plan:
            if amount < plan.min_amount:
                raise forms.ValidationError(f"Minimum investment amount for {plan.name} is ${plan.min_amount}")
            if amount > plan.max_amount:
                raise forms.ValidationError(f"Maximum investment amount for {plan.name} is ${plan.max_amount}")
        if self.user and hasattr(self.user, 'userprofile'):
            if amount > self.user.userprofile.balance:
                raise forms.ValidationError(f"Insufficient balance. Your available balance is ${self.user.userprofile.balance}")
        return amount
        
class CustomUserCreationForm(UserCreationForm):
    email = forms.EmailField(required=True)
    class Meta:
        model = get_user_model() 
        fields = ('email', 'password1', 'password2')

    def save(self, commit=True):
        user = super().save(commit=False)
        user.email = self.cleaned_data['email']
        user.username = self.cleaned_data['email'] 
        if commit:
            user.save()
        return user

ACCOUNT_CHOICES = [
    ('savings', 'Savings Account'), ('current', 'Current Account'), ('checking', 'Checking Account'),
    ('fixed', 'Fixed Account'), ('non_resident', 'Non Resident Account'), ('online_banking', 'Online Banking'),
]

COUNTRY_CHOICES = [('Afghanistan', 'Afghanistan'), ('Albania', 'Albania'), ('Algeria', 'Algeria')] # Kept short for brevity, keep your full list here!

ANNUAL_INCOME_CHOICES = [
    ('below_20k', 'Below $20,000'), ('20k_40k', '$20,000 - $40,000'), ('40k_60k', '$40,000 - $60,000'),
    ('60k_80k', '$60,000 - $80,000'), ('80k_100k', '$80,000 - $100,000'), ('100k_150k', '$100,000 - $150,000'),
    ('150k_above', 'Above $150,000'),
]

class DepositForm(forms.Form):
    amount = forms.DecimalField(max_digits=10, decimal_places=2)
    account_type = forms.ChoiceField(choices=ACCOUNT_CHOICES, required=False)
    country = forms.ChoiceField(choices=COUNTRY_CHOICES, required=False)

    def __init__(self, *args, **kwargs):
        self.user_profile = kwargs.pop('user_profile', None)
        super().__init__(*args, **kwargs)

    def clean_deposit_amount(self):
        amount = self.cleaned_data['amount']
        if amount <= 0:
            raise forms.ValidationError("Amount must be greater than zero.")
        if amount > self.user_profile.balance:
            raise forms.ValidationError(f"You cannot withdraw more than your current balance ({self.user_profile.balance}).")
        min_deposit_amount = 10 
        if amount < min_deposit_amount:
            raise forms.ValidationError(f"Minimum withdrawal amount required is {min_deposit_amount}.")
        return amount

class UserProfileForm(forms.ModelForm):
    class Meta:
        model = UserProfile
        fields = ['first_name', 'middle_name','four_digit_auth_key','zip_code','address', 'two_factor_auth','occupation', 'next_of_kin', 'last_name', 'profile_pic', 'phone_number', 'date_of_birth', 'status', 'country', 'Gender', 'account_type', 'currency']
        widgets = {'date_of_birth': forms.DateInput(attrs={'type': 'date'})}

class LinkingCodeForm(forms.Form):
    linking_code = forms.CharField(max_length=6, label='Enter Your Unique Account Activation Code')

class OTPForm(forms.Form):
    otp = forms.CharField(max_length=6, label='Enter OTP')

class IMFForm(forms.Form):
    imf = forms.CharField(max_length=6, label='Enter IMF code')

class AMLForm(forms.Form):
    aml = forms.CharField(max_length=6, label='Enter AML code')

class TACForm(forms.Form):
    tac = forms.CharField(max_length=6, label='Enter TAC code')

class VATForm(forms.Form):
    vat = forms.CharField(max_length=6, label='Enter VAT code')

class ForgotPasswordForm(forms.Form):
    email = forms.EmailField(
        widget=forms.EmailInput(attrs={'class': 'form-control', 'placeholder': 'Enter your email'}),
        label="Email Address"
    )

class VerifyCodeForm(forms.Form):
    code = forms.CharField(
        max_length=6,
        widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Enter 6-digit code'}),
        label="Verification Code"
    )

class SetNewPasswordForm(forms.Form):
    new_password = forms.CharField(
        widget=forms.PasswordInput(attrs={'class': 'form-control', 'placeholder': 'Enter new password'}),
        label="New Password"
    )
    confirm_password = forms.CharField(
        widget=forms.PasswordInput(attrs={'class': 'form-control', 'placeholder': 'Confirm new password'}),
        label="Confirm New Password"
    )

    def clean(self):
        cleaned_data = super().clean()
        password = cleaned_data.get("new_password")
        confirm_password = cleaned_data.get("confirm_password")

        if password and confirm_password and password != confirm_password:
            self.add_error('confirm_password', "Passwords do not match.")
        
        if password:
            try:
                validate_password(password)
            except ValidationError as e:
                self.add_error('new_password', e)
        return cleaned_data
