import random
import re
import traceback
from datetime import date, datetime, timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth import authenticate, get_user_model, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.tokens import default_token_generator
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.core.mail import EmailMessage, send_mail
from django.core.signing import BadSignature, SignatureExpired, TimestampSigner
from django.db import transaction
from django.db.models import Avg, Count, Q, Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.encoding import force_str
from django.utils.http import urlsafe_base64_decode
from docx import Document

from .decorators import *
from .forms import *
from .models import *
from .utilis import *

User = get_user_model()
signer = TimestampSigner()

# ==========================================
# 1. PASSWORD RESET VIEWS
# ==========================================

def forgot_password_request(request):
    if request.method == 'POST':
        form = ForgotPasswordForm(request.POST)
        if form.is_valid():
            email = form.cleaned_data['email']
            try:
                user = User.objects.get(email=email)
            except User.DoesNotExist:
                messages.success(request, "If an account with that email exists, a reset code has been sent.")
                return redirect('verify_reset_code')

            otp = str(random.randint(100000, 999999))
            request.session['reset_email'] = email
            request.session['reset_otp'] = otp
            request.session['reset_verified'] = False

            subject = "Password Reset Verification Code"
            message = f"Hello,\n\nYour password reset code is: {otp}\n\nIf you did not request this, please ignore this email.\n\nAxos Bank Team"
            from_email = settings.DEFAULT_FROM_EMAIL
            recipient_list = [email]

            try:
                send_mail(subject, message, from_email, recipient_list, fail_silently=False)
                messages.success(request, "A verification code has been sent to your email.")
                return redirect('verify_reset_code')
            except Exception as e:
                messages.error(request, f"Failed to send email. Error: {str(e)}")
    else:
        form = ForgotPasswordForm()

    return render(request, 'BankApp/forgot_password.html', {'form': form})


def verify_reset_code(request):
    if 'reset_email' not in request.session:
        messages.error(request, "Please enter your email first.")
        return redirect('forgot_password_request')

    if request.method == 'POST':
        form = VerifyCodeForm(request.POST)
        if form.is_valid():
            entered_code = form.cleaned_data['code']
            session_code = request.session.get('reset_otp')

            if entered_code == session_code:
                request.session['reset_verified'] = True
                messages.success(request, "Code verified! Please set your new password.")
                return redirect('set_new_password')
            else:
                messages.error(request, "Invalid verification code. Please try again.")
    else:
        form = VerifyCodeForm()

    return render(request, 'BankApp/verify_code.html', {'form': form})


def set_new_password(request):
    if not request.session.get('reset_verified', False):
        messages.error(request, "Unauthorized access. Please verify your email first.")
        return redirect('forgot_password_request')

    if request.method == 'POST':
        form = SetNewPasswordForm(request.POST)
        if form.is_valid():
            new_password = form.cleaned_data['new_password']
            email = request.session.get('reset_email')

            try:
                user = User.objects.get(email=email)
                user.set_password(new_password)
                user.save()

                del request.session['reset_email']
                del request.session['reset_otp']
                del request.session['reset_verified']

                messages.success(request, "Your password has been successfully reset! You can now log in.")
                return redirect('user_login')
            except User.DoesNotExist:
                messages.error(request, "Something went wrong. User not found.")
                return redirect('user_login')
    else:
        form = SetNewPasswordForm()

    return render(request, 'BankApp/set_new_password.html', {'form': form})

# ==========================================
# 2. LOAN & KYC VIEWS
# ==========================================

def submit_loan(request):
    if request.method == "POST":
        form = LoanForm(request.POST)

        if form.is_valid():
            loan = form.save(commit=False)
            loan.user = request.user
            rate, total = calculate_interest(loan.loan_amount, loan.loan_duration)
            loan.interest_rate = rate
            loan.total_amount_due = total
            loan.save()

            pdf_path = generate_loan_pdf(loan) 
            email_pdf(request.user.email, pdf_path)

            messages.success(request, "Loan submitted successfully! Approval sent to email.")
            return redirect("dashboard")
    else:
        form = LoanForm()

    return render(request, "BankApp/submit_loans.html", {"form": form})


def send_kyc_email(request, user_id):
    user = get_object_or_404(User, id=user_id)
    try:
        kyc = KYC.objects.get(user=user)
    except KYC.DoesNotExist:
        kyc = None

    subject = "Your KYC Verification Status"
    message = (
        f"Dear {user.first_name},\n\n"
        "Thank you for submitting your KYC details. "
        "Your verification is being reviewed.\n\n"
        "Best regards,\nYour Bank Team"
    )

    email = EmailMessage(subject, message, settings.EMAIL_HOST_USER, [user.email])
    email.send()

    messages.success(request, "KYC email sent successfully.")
    return redirect("dashboard")


def download_kyc_pdf(request, user_id):
    user_profile = get_object_or_404(UserProfile, user_id=user_id)
    doc = Document()
    doc.add_heading("KYC Details", level=1)

    doc.add_paragraph(f"Full Name: {user_profile.full_name}")
    doc.add_paragraph(f"Email: {user_profile.user.email}")
    doc.add_paragraph(f"Phone: {user_profile.phone}")
    doc.add_paragraph(f"Address: {user_profile.address}")
    doc.add_paragraph(f"Occupation: {user_profile.occupation}")
    doc.add_paragraph(f"City: {user_profile.city}")
    doc.add_paragraph(f"ZIP Code: {user_profile.zip_code}")

    file_name = f"KYC_{user_profile.user.email}.docx"
    file_path = f"/tmp/{file_name}"
    doc.save(file_path)

    with open(file_path, "rb") as file:
        response = HttpResponse(file.read(), content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        response["Content-Disposition"] = f'attachment; filename="{file_name}"'
        return response


@staff_member_required
def manage_loans(request):
    loans = Loan.objects.all().order_by('-submitted_at')
    status_filter = request.GET.get('status', '')
    search_query = request.GET.get('q', '')
    
    if status_filter:
        loans = loans.filter(status__iexact=status_filter)
    if search_query:
        loans = loans.filter(
            Q(user__username__icontains=search_query) |
            Q(user__email__icontains=search_query) |
            Q(loan_type__icontains=search_query) |
            Q(purpose__icontains=search_query) |
            Q(user__first_name__icontains=search_query) |
            Q(user__last_name__icontains=search_query)
        )
    
    total_loans = Loan.objects.count()
    pending_loans = Loan.objects.filter(status__iexact='Pending').count()
    approved_loans = Loan.objects.filter(status__iexact='Approved').count()
    rejected_loans = Loan.objects.filter(status__iexact='Rejected').count()
    
    for loan in loans:
        try:
            amount_decimal = Decimal(str(loan.amount))
            loan.processing_fee = amount_decimal * Decimal('0.05')
            loan.total_amount_due = amount_decimal + loan.processing_fee
            loan.processing_fee_display = f"${loan.processing_fee:,.2f}"
            loan.total_amount_due_display = f"${loan.total_amount_due:,.2f}"
            loan.amount_display = f"${loan.amount:,.2f}"
        except Exception:
            loan.processing_fee = Decimal('0.00')
            loan.total_amount_due = loan.amount
            loan.processing_fee_display = "$0.00"
            loan.total_amount_due_display = f"${loan.amount:,.2f}"
            loan.amount_display = f"${loan.amount:,.2f}"
        
        if loan.user:
            loan.full_name = f"{loan.user.first_name} {loan.user.last_name}".strip() or loan.user.username
            loan.email = loan.user.email
        else:
            loan.full_name = "Unknown User"
            loan.email = "N/A"
        
        loan.loan_purpose = loan.purpose if loan.purpose else loan.loan_type
    
    if request.method == 'POST' and 'action' in request.POST:
        loan_ids = request.POST.getlist('loan_ids')
        action = request.POST.get('action')
        
        if loan_ids and action:
            loans_to_process = Loan.objects.filter(id__in=loan_ids)
            if action == 'approve':
                loans_to_process.update(status='Approved')
                messages.success(request, f'{len(loan_ids)} loan(s) approved successfully.')
            elif action == 'reject':
                loans_to_process.update(status='Rejected')
                messages.success(request, f'{len(loan_ids)} loan(s) rejected.')
            return redirect('manage_loans')
    
    context = {
        'loans': loans, 'total_count': total_loans, 'pending_count': pending_loans,
        'approved_count': approved_loans, 'rejected_count': rejected_loans,
        'status_filter': status_filter, 'search_query': search_query,
        'status_choices': ['Pending', 'Approved', 'Rejected'],
    }
    return render(request, 'BankApp/manage_loans.html', context)


@staff_member_required
def approve_loan(request, loan_id):
    try:
        loan = get_object_or_404(Loan, id=loan_id)
        if loan.status == 'Approved':
            messages.warning(request, f"Loan #{loan.id} is already approved.")
            return redirect('manage_loans')
        
        loan.status = 'Approved'
        loan.reviewed_at = datetime.now()
        loan.save()
        
        amount_decimal = Decimal(str(loan.amount))
        processing_fee = amount_decimal * Decimal('0.05')
        total_due = amount_decimal + processing_fee
        user_email = loan.user.email if loan.user else None
        
        if user_email:
            try:
                send_mail(
                    subject="Loan Approved - Axos Finance",
                    message=(
                        f"Hello {loan.user.get_full_name() or loan.user.username},\n\n"
                        f"Congratulations! Your loan application (ID: #{loan.id}) has been approved.\n\n"
                        f"Amount: ${loan.amount:,.2f}\nLoan Type: {loan.loan_type}\n"
                        f"Duration: {loan.duration} months\nInterest Rate: {loan.interest}%\n"
                        f"Processing Fee (5%): ${processing_fee:,.2f}\nTotal Amount Due: ${total_due:,.2f}\n\n"
                        f"Best regards,\nAxos Finance Team\nsupport@axosbank.com"
                    ),
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    recipient_list=[user_email],
                    fail_silently=False,
                )
                messages.success(request, f"Loan #{loan.id} approved and notification email sent.")
            except Exception as e:
                messages.warning(request, f"Loan approved but email failed to send: {str(e)}")
        else:
            messages.success(request, f"Loan #{loan.id} approved.")
    except Loan.DoesNotExist:
        messages.error(request, "Loan not found.")
    except Exception as e:
        messages.error(request, f"Error approving loan: {str(e)}")
    
    return redirect('manage_loans')


@staff_member_required
def reject_loan(request, loan_id):
    try:
        loan = get_object_or_404(Loan, id=loan_id)
        if loan.status == 'Rejected':
            messages.warning(request, f"Loan #{loan.id} is already rejected.")
            return redirect('manage_loans')
        
        loan.status = 'Rejected'
        loan.reviewed_at = datetime.now()
        loan.save()
        
        user_email = loan.user.email if loan.user else None
        rejection_reason = request.POST.get('rejection_reason', "Your application did not meet our current lending criteria.")
        
        if user_email:
            try:
                send_mail(
                    subject="Loan Application Update - Axos Finance",
                    message=(
                        f"Hello {loan.user.get_full_name() or loan.user.username},\n\n"
                        f"We regret to inform you that your loan application (ID: #{loan.id}) "
                        f"for ${loan.amount:,.2f} has not been approved at this time.\n\n"
                        f"Reason: {rejection_reason}\n\n"
                        f"Best regards,\nAxos Finance Team"
                    ),
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    recipient_list=[user_email],
                    fail_silently=False,
                )
                messages.error(request, f"Loan #{loan.id} rejected and notification email sent.")
            except Exception as e:
                messages.warning(request, f"Loan rejected but email failed to send: {str(e)}")
        else:
            messages.error(request, f"Loan #{loan.id} rejected.")
    except Loan.DoesNotExist:
        messages.error(request, "Loan not found.")
    except Exception as e:
        messages.error(request, f"Error rejecting loan: {str(e)}")
    
    return redirect('manage_loans')


@staff_member_required
def view_loan_details(request, loan_id):
    loan = get_object_or_404(Loan, id=loan_id)
    processing_fee = loan.loan_amount * 0.05
    total_due = loan.loan_amount + processing_fee
    return render(request, 'BankApp/loan_details.html', {'loan': loan, 'processing_fee': processing_fee, 'total_due': total_due})


def calculate_interest(amount, duration, loan_type=None, annual_income=None):
    base_rate = 5.0 
    if loan_type:
        loan_type_lower = loan_type.lower()
        if 'mortgage' in loan_type_lower: base_rate = 3.5
        elif 'auto' in loan_type_lower: base_rate = 4.5
        elif 'education' in loan_type_lower: base_rate = 4.0
        elif 'emergency' in loan_type_lower: base_rate = 10.0
        elif 'business' in loan_type_lower: base_rate = 6.5
        elif 'personal' in loan_type_lower: base_rate = 7.5
    
    if duration > 60: base_rate += 0.5
    if annual_income and amount:
        try:
            income_ratio = float(amount) / float(annual_income)
            if income_ratio > 2: base_rate += 1.0
            elif income_ratio < 0.5: base_rate -= 0.5
        except (ValueError, TypeError): pass
    
    total = float(amount) * (1 + (base_rate / 100) * (duration / 12))
    return base_rate, round(total, 2)

# ==========================================
# 3. ADMIN DASHBOARD & LOAN APPLICATIONS
# ==========================================

@staff_member_required
def admin_dashboard(request):
    total_users = UserProfile.objects.count()
    active_users = UserProfile.objects.filter(user__is_active=True).count()
    total_loans = Loan.objects.count()
    pending_loans = Loan.objects.filter(status='Pending').count()
    approved_loans = Loan.objects.filter(status='Approved').count()
    rejected_loans = Loan.objects.filter(status='Rejected').count()
    
    today = date.today()
    today_loans = Loan.objects.filter(submitted_at__date=today).count()
    week_ago = datetime.now() - timedelta(days=7)
    this_week_loans = Loan.objects.filter(submitted_at__gte=week_ago).count()
    month_ago = datetime.now() - timedelta(days=30)
    this_month_loans = Loan.objects.filter(submitted_at__gte=month_ago).count()
    
    total_loan_amount = Loan.objects.aggregate(total=Sum('amount'))['total'] or Decimal('0')
    total_pending_amount = Loan.objects.filter(status='Pending').aggregate(total=Sum('amount'))['total'] or Decimal('0')
    total_approved_amount = Loan.objects.filter(status='Approved').aggregate(total=Sum('amount'))['total'] or Decimal('0')
    total_rejected_amount = Loan.objects.filter(status='Rejected').aggregate(total=Sum('amount'))['total'] or Decimal('0')
    avg_loan_amount = Loan.objects.aggregate(avg=Avg('amount'))['avg'] or Decimal('0')
    
    loan_type_stats = Loan.objects.values('loan_type').annotate(count=Count('id'), total_amount=Sum('amount'), avg_amount=Avg('amount')).order_by('-total_amount')
    status_distribution = [
        {'status': 'Pending', 'count': pending_loans, 'color': '#f59e0b'},
        {'status': 'Approved', 'count': approved_loans, 'color': '#10b981'},
        {'status': 'Rejected', 'count': rejected_loans, 'color': '#ef4444'},
    ]
    duration_stats = Loan.objects.values('duration').annotate(count=Count('id'), avg_amount=Avg('amount')).order_by('duration')
    recent_pending_loans = Loan.objects.filter(status='Pending').select_related('user').order_by('-submitted_at')[:5]
    
    for loan in recent_pending_loans:
        try:
            amount_decimal = Decimal(str(loan.amount))
            loan.processing_fee = amount_decimal * Decimal('0.05')
            loan.total_due = amount_decimal + loan.processing_fee
        except:
            loan.processing_fee = Decimal('0')
            loan.total_due = loan.amount
    
    recent_approved_loans = Loan.objects.filter(status='Approved').select_related('user').order_by('-reviewed_at')[:5]
    total_processed = approved_loans + rejected_loans
    approval_rate = (approved_loans / total_processed * 100) if total_processed > 0 else 0
    
    monthly_trends = []
    for i in range(6, -1, -1):
        month_start = datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0) - timedelta(days=30*i)
        month_end = month_start + timedelta(days=30)
        month_loans = Loan.objects.filter(submitted_at__gte=month_start, submitted_at__lt=month_end).aggregate(count=Count('id'), amount=Sum('amount'))
        monthly_trends.append({'month': month_start.strftime('%b %Y'), 'count': month_loans['count'] or 0, 'amount': month_loans['amount'] or Decimal('0')})
    
    top_loans = Loan.objects.select_related('user').order_by('-amount')[:5]
    purpose_stats = Loan.objects.exclude(purpose__isnull=True).exclude(purpose='').values('purpose').annotate(count=Count('id')).order_by('-count')[:10]
    
    context = {
        'total_users': total_users, 'active_users': active_users, 'total_loans': total_loans,
        'pending_loans': pending_loans, 'approved_loans': approved_loans, 'rejected_loans': rejected_loans,
        'today_loans': today_loans, 'this_week_loans': this_week_loans, 'this_month_loans': this_month_loans,
        'total_loan_amount': total_loan_amount, 'total_pending_amount': total_pending_amount,
        'total_approved_amount': total_approved_amount, 'total_rejected_amount': total_rejected_amount,
        'avg_loan_amount': avg_loan_amount, 'approval_rate': round(approval_rate, 1),
        'loan_type_stats': loan_type_stats, 'status_distribution': status_distribution,
        'duration_stats': duration_stats, 'purpose_stats': purpose_stats, 'monthly_trends': monthly_trends,
        'recent_pending_loans': recent_pending_loans, 'recent_approved_loans': recent_approved_loans,
        'top_loans': top_loans, 'now': datetime.now(),
    }
    return render(request, 'BankApp/admin_dashboard.html', context)


def check_staff_status(request):
    if request.user.is_authenticated:
        user_info = {
            'username': request.user.username, 'email': request.user.email,
            'is_staff': request.user.is_staff, 'is_superuser': request.user.is_superuser,
        }
        messages.info(request, f"User info: {user_info}")
    return redirect('manage_loans')


@login_required
def apply_loan(request):
    try:
        user_profile = UserProfile.objects.get(user=request.user)
    except UserProfile.DoesNotExist:
        user_profile = UserProfile.objects.create(user=request.user)
    
    if request.method == "POST":
        form = LoanForm(request.POST)
        if form.is_valid():
            amount = form.cleaned_data['amount']
            loan_type = form.cleaned_data['loan_type']
            duration = form.cleaned_data['duration']
            purpose = form.cleaned_data.get('purpose', 'other')
            employment_status = form.cleaned_data.get('employment_status', 'employed')
            annual_income = form.cleaned_data.get('annual_income', 0)
            repayment_frequency = form.cleaned_data.get('repayment_frequency', 'monthly')
            collateral = form.cleaned_data.get('collateral', '')
            requested_date = form.cleaned_data.get('requested_date', date.today())
            notes = form.cleaned_data.get('notes', '')
            
            interest_rate, total = calculate_interest(float(amount), duration, loan_type, annual_income)
            
            loan_data = {
                'amount': float(amount), 'loan_type': loan_type, 'duration': duration,
                'interest': interest_rate, 'total': float(total),
                'purpose': purpose, 'employment_status': employment_status,
                'annual_income': float(annual_income) if annual_income else 0,
                'repayment_frequency': repayment_frequency, 'collateral': collateral,
                'requested_date': requested_date.isoformat(), 'notes': notes
            }
            request.session['loan_data'] = loan_data
            return redirect('loan_review')
        else:
            messages.error(request, "Please correct the errors below.")
    else:
        initial_data = {'requested_date': date.today()}
        if hasattr(user_profile, 'annual_income') and user_profile.annual_income:
            initial_data['annual_income'] = user_profile.annual_income
        form = LoanForm(initial=initial_data)

    return render(request, 'BankApp/loans.html', {'form': form, 'user_profile': user_profile, 'page_title': 'Apply for Loan'})


@login_required
def loan_review(request):
    data = request.session.get('loan_data')
    if not data:
        messages.warning(request, "No loan application data found. Please start a new application.")
        return redirect('apply_loan')

    duration_value = data.get('duration', '')
    numeric_duration = int(re.findall(r'\d+', str(duration_value))[0]) if re.findall(r'\d+', str(duration_value)) else 0
    
    loan_amount = float(data.get('amount', 0))
    processing_fee = round(loan_amount * 0.05, 2)
    total = float(data.get('total', 0))
    monthly_installment = total / numeric_duration if numeric_duration > 0 else 0
    annual_income = float(data.get('annual_income', 0))
    loan_to_income_ratio = (loan_amount / annual_income) * 100 if annual_income > 0 else 0

    if request.method == "POST":
        loan_data_for_db = {
            'user': request.user, 'amount': data['amount'], 'loan_type': data['loan_type'],
            'duration': numeric_duration, 'interest': data['interest'],
            'total_payable': data['total'], 'status': "Pending",
        }
        
        for field in ['purpose', 'employment_status', 'annual_income', 'repayment_frequency', 'collateral', 'notes']:
            if field in data:
                if field == 'requested_date':
                    try: loan_data_for_db[field] = datetime.fromisoformat(data[field]).date()
                    except: loan_data_for_db[field] = date.today()
                elif field == 'annual_income': loan_data_for_db[field] = float(data[field])
                else: loan_data_for_db[field] = data[field]
        
        loan = Loan.objects.create(**loan_data_for_db)

        try:
            send_mail(
                subject="Loan Application Submitted",
                message=f"Your loan application for ${data['amount']} ({data.get('loan_type', 'Personal')}) has been submitted.\n\nApplication ID: {loan.id}",
                from_email=settings.DEFAULT_FROM_EMAIL, recipient_list=[request.user.email], fail_silently=True,
            )
        except Exception as e: print(f"Email sending failed: {e}")

        del request.session['loan_data']
        messages.success(request, f"Loan application submitted successfully! Your application ID is {loan.id}.")
        return redirect('loan_pending')

    context = {**data, 'user': request.user, 'numeric_duration': numeric_duration, 'processing_fee': processing_fee, 'monthly_installment': round(monthly_installment, 2), 'loan_to_income_ratio': round(loan_to_income_ratio, 1)}
    if 'requested_date' in data:
        try: context['requested_date_formatted'] = datetime.fromisoformat(data['requested_date']).strftime("%B %d, %Y")
        except: context['requested_date_formatted'] = "Not specified"
    return render(request, 'BankApp/loan_review.html', context)


@login_required
def loan_pending(request):
    recent_loan = Loan.objects.filter(user=request.user, status='Pending').order_by('-submitted_at').first()
    user_loans = Loan.objects.filter(user=request.user).order_by('-submitted_at')[:5]
    return render(request, 'BankApp/loan_pending.html', {'recent_loan': recent_loan, 'user_loans': user_loans})


@login_required
def loan_success(request, loan_id):
    loan = get_object_or_404(Loan, id=loan_id, user=request.user)
    return render(request, 'BankApp/loan_success.html', {'loan': loan})


@login_required
def loan_approved(request, loan_id):
    loan = Loan.objects.get(id=loan_id, user=request.user)
    return render(request, 'loan_approved.html', {'loan': loan})

# ==========================================
# 4. AUTHENTICATION & PROFILE
# ==========================================

@unauthenticated_user
def register(request):
    if request.method == 'POST':
        form = CustomUserCreationForm(request.POST)
        if form.is_valid():
            try:
                user = form.save(commit=False)
                user.is_active = True
                user.save()
                profile, created = UserProfile.objects.get_or_create(user=user)
                profile.is_email_verified = True
                profile.save()
                messages.success(request, 'Your account has been created successfully. You can now sign in.')
                return redirect('user_login')
            except Exception:
                messages.error(request, 'We could not complete your registration. Please try again.')
    else:
        form = CustomUserCreationForm()
    return render(request, 'BankApp/register.html', {'form': form})
    

def verify_email(request, signed_value):
    try:
        user_id = signer.unsign(signed_value, max_age=604800)
        user = User.objects.get(pk=user_id)
    except SignatureExpired:
        messages.error(request, "Verification link has expired. Please request a new one.")
        return redirect("register")
    except (BadSignature, User.DoesNotExist):
        messages.error(request, "Invalid verification link.")
        return redirect("register")

    profile = user.userprofile
    profile.is_email_verified = True
    profile.save()
    user.is_active = True
    user.save()
    messages.success(request, "Email verified successfully! You may now log in.")
    return redirect("user_login")


@unauthenticated_user
def user_login(request):  
    if request.method == 'POST':
        email = request.POST.get('email')
        password = request.POST.get('password')
        user = authenticate(request, email=email, password=password)
        if user is not None:
            if not user.is_active:
                messages.error(request, "Please verify your email before logging in.")
                return redirect('user_login')
            login(request, user)
            return redirect('dashboard')
        else:
            messages.error(request, 'Email or Password is incorrect.')
    return render(request, 'BankApp/login.html')


def LogOut(request):
    logout(request)
    return redirect('user_login')


@login_required(login_url='user_login')
def profile(request):
    try:
        user_profile = UserProfile.objects.get(user=request.user)
    except UserProfile.DoesNotExist:
        user_profile = UserProfile.objects.create(user=request.user)
    return render(request, 'BankApp/profile.html', {'user_profile':user_profile})


@login_required(login_url='user_login')
@transaction.atomic
def reset_profile(request):
    try:
        profile = request.user.userprofile
    except UserProfile.DoesNotExist:
        profile = UserProfile(user=request.user)
        profile.save()

    if request.method == 'POST':
        form = UserProfileForm(request.POST, request.FILES, instance=profile)
        if form.is_valid():
            try:
                form.save()
                messages.success(request, "Profile updated successfully.")
                return redirect('dashboard')
            except Exception as e:
                messages.error(request, f"An unexpected error occurred: {e}")
        else:
            messages.error(request, "Please correct the errors below.")
    else:
        form = UserProfileForm(instance=profile)
    return render(request, 'BankApp/update_profile.html', {'form': form})


@login_required(login_url='user_login')
def Upgrade_Account(request):
    try:
        user_profile = UserProfile.objects.get(user=request.user)
    except UserProfile.DoesNotExist:
        user_profile = UserProfile.objects.create(user=request.user)
    message = 'Account upgraded successfully' if user_profile.is_upgraded else 'Account upgrade processing contact support for more information'
    return render(request, 'BankApp/account_upgrade.html', {'user_profile': user_profile, 'message': message})

# ==========================================
# 5. KYC & INVESTMENTS
# ==========================================

@login_required
def kyc(request):
    try:
        kyc = KYC.objects.get(user=request.user)
    except KYC.DoesNotExist:
        kyc = None

    if request.method == 'POST':
        form = KYCForm(request.POST, request.FILES, instance=kyc)
        if form.is_valid():
            kyc = form.save(commit=False)
            kyc.user = request.user
            kyc.status = "Pending"
            kyc.save()
            messages.success(request, "KYC submitted successfully.")
            return redirect('dashboard')
    else:
        form = KYCForm(instance=kyc)
    return render(request, 'BankApp/kyc.html', {"form": form})


@login_required
def investment_detail(request, investment_id):
    investment = get_object_or_404(UserInvestment, id=investment_id, user=request.user)
    try:
        from .models import InvestmentTransaction
        transactions = InvestmentTransaction.objects.filter(investment=investment).order_by('-created_at')
    except ImportError:
        transactions = []
    
    user_profile = get_object_or_404(UserProfile, user=request.user)
    today = timezone.now()
    start_date = investment.start_date
    end_date = investment.end_date
    total_days = (end_date - start_date).days
    days_passed = (today - start_date).days
    days_remaining = max(0, (end_date - today).days)
    progress_percentage = min(100, max(0, (days_passed / total_days) * 100)) if total_days > 0 else (100 if investment.status.lower() == 'completed' else 0)
    investment_status = investment.status
    if investment_status.lower() == 'active' and days_remaining <= 0:
        investment_status = 'COMPLETED'

    plan = investment.investment_plan
    min_expected_return = investment.min_expected_return or Decimal('0')
    max_expected_return = investment.max_expected_return or Decimal('0')
    
    if investment_status.lower() == 'completed':
        current_value = investment.actual_return or max_expected_return
    else:
        initial_investment = Decimal(str(investment.amount_invested))
        min_expected_profit = min_expected_return - initial_investment
        max_expected_profit = max_expected_return - initial_investment
        current_min_profit = min_expected_profit * Decimal(str(progress_percentage / 100))
        current_max_profit = max_expected_profit * Decimal(str(progress_percentage / 100))
        current_profit = (current_min_profit + current_max_profit) / 2
        current_value = initial_investment + current_profit

    profit_loss = current_value - Decimal(str(investment.amount_invested))
    avg_profit_percentage = (plan.min_profit_percentage + plan.max_profit_percentage) / 2 if plan else Decimal('0')
    roi_percentage = (profit_loss / Decimal(str(investment.amount_invested))) * 100 if investment.amount_invested > 0 else Decimal('0')

    context = {
        'investment': investment, 'transactions': transactions, 'user_profile': user_profile,
        'progress_percentage': round(progress_percentage, 1), 'days_remaining': days_remaining,
        'total_days': total_days, 'days_passed': days_passed, 'investment_status': investment_status,
        'current_value': round(current_value, 2), 'profit_loss': round(profit_loss, 2),
        'roi_percentage': round(roi_percentage, 2), 'min_expected_return': round(min_expected_return, 2),
        'max_expected_return': round(max_expected_return, 2), 'avg_profit_percentage': round(avg_profit_percentage, 2),
        'profit_range': f"{plan.min_profit_percentage}% - {plan.max_profit_percentage}%" if plan else "N/A",
        'duration_display': plan.duration_display if plan else "N/A",
        'currency_symbol': getattr(user_profile, 'currency', '$')
    }
    return render(request, 'BankApp/investment_detail.html', context)


@login_required
def investment_plans(request):
    starter_plans = InvestmentPlan.objects.filter(is_active=True, plan_type='STARTER').order_by('investment_type')
    pro_plans = InvestmentPlan.objects.filter(is_active=True, plan_type='PRO').order_by('investment_type')
    elite_plans = InvestmentPlan.objects.filter(is_active=True, plan_type='ELITE').order_by('investment_type')
    user_investments = UserInvestment.objects.filter(user=request.user).select_related('investment_plan').order_by('-start_date')
    user_profile = get_object_or_404(UserProfile, user=request.user)
    
    context = {
        'starter_plans': starter_plans, 'pro_plans': pro_plans, 'elite_plans': elite_plans,
        'user_investments': user_investments, 'user_profile': user_profile,
    }
    return render(request, 'BankApp/investment_plan.html', context)


@login_required
def create_investment(request):
    user_profile = get_object_or_404(UserProfile, user=request.user)
    plan_id = request.GET.get('plan_id')
    initial_data = {}
    
    if plan_id:
        try:
            plan = InvestmentPlan.objects.get(id=plan_id, is_active=True)
            initial_data['investment_plan'] = plan
            initial_data['amount_invested'] = plan.min_amount
        except InvestmentPlan.DoesNotExist:
            messages.error(request, "Selected investment plan is not available")
            return redirect('investment_plans')
    
    if request.method == 'POST':
        form = InvestmentForm(request.POST, user=request.user)
        if form.is_valid():
            try:
                with transaction.atomic():
                    plan = form.cleaned_data['investment_plan']
                    amount = form.cleaned_data['amount_invested']
                    
                    if amount < plan.min_amount:
                        messages.error(request, f"Minimum investment for {plan.name} is ${plan.min_amount}")
                        return redirect('create_investment')
                    if amount > plan.max_amount:
                        messages.error(request, f"Maximum investment for {plan.name} is ${plan.max_amount}")
                        return redirect('create_investment')
                    if amount > user_profile.balance:
                        messages.error(request, f"Insufficient balance. Your available balance is ${user_profile.balance}")
                        return redirect('create_investment')
                    
                    if plan.investment_type == 'SHORT_TERM' and plan.interval_hours:
                        end_date = timezone.now() + timedelta(hours=plan.interval_hours)
                    else:
                        end_date = timezone.now() + timedelta(days=plan.duration_days)
                    
                    investment = form.save(commit=False)
                    investment.user = request.user
                    investment.end_date = end_date
                    investment.status = 'ACTIVE'
                    investment.save()
                    
                    user_profile.balance -= amount
                    user_profile.save()

                    try:
                        from .models import InvestmentTransaction
                        transaction_fields = [f.name for f in InvestmentTransaction._meta.fields]
                        transaction_data = {
                            'user': request.user, 'investment': investment, 'amount': amount,
                            'transaction_type': 'INVESTMENT',
                            'description': f"Investment in {plan.name} (Profit Range: {plan.min_profit_percentage}%-{plan.max_profit_percentage}%)"
                        }
                        if 'status' in transaction_fields:
                            transaction_data['status'] = 'COMPLETED'
                        InvestmentTransaction.objects.create(**transaction_data)
                    except Exception as e:
                        print(f"Transaction creation error (non-critical): {e}")

                    min_profit = amount * (plan.min_profit_percentage / 100)
                    max_profit = amount * (plan.max_profit_percentage / 100)
                    avg_profit = (min_profit + max_profit) / 2
                    
                    duration_text = f"{plan.interval_hours} hours" if plan.investment_type == 'SHORT_TERM' and plan.interval_hours else f"{plan.duration_days} days"
                    
                    messages.success(request, f"🎉 Investment Successful! Plan: {plan.name}, Amount: ${amount:,.2f}, Profit Range: ${min_profit:,.2f} - ${max_profit:,.2f}, Duration: {duration_text}, Status: Active")
                    request.session['investment_success'] = True
                    request.session['investment_id'] = investment.id
                    return redirect('investment_dashboard')

            except Exception as e:
                messages.error(request, f"❌ Error creating investment: {str(e)}")
                print(f"Investment creation error: {traceback.format_exc()}")
        else:
            for field, errors in form.errors.items():
                for error in errors:
                    messages.error(request, f"⚠️ {field}: {error}")
    else:
        form = InvestmentForm(user=request.user, initial=initial_data)

    return render(request, 'BankApp/investment_create.html', {'form': form, 'user_profile': user_profile, 'plan_id': plan_id})


@login_required
def investment_dashboard(request):
    user_profile = get_object_or_404(UserProfile, user=request.user)
    investment_success = request.session.pop('investment_success', False)
    investment_id = request.session.pop('investment_id', None)
    
    if investment_success and investment_id:
        try:
            investment = UserInvestment.objects.get(id=investment_id, user=request.user)
            messages.success(request, f"🎉 Investment #{investment.id} created successfully! Check your active investments below.")
        except UserInvestment.DoesNotExist:
            pass
    
    active_investments = UserInvestment.objects.filter(user=request.user, status='ACTIVE').order_by('-created_at')
    active_investments_data = [{
        'investment': inv, 'progress_percentage': inv.progress_percentage,
        'days_remaining': inv.days_remaining, 'current_value': inv.current_value,
        'current_profit': inv.current_profit, 'roi_percentage': inv.roi_percentage,
        'profit_range_display': inv.profit_range_display, 'expected_return_range': inv.expected_return_range,
    } for inv in active_investments]
    
    completed_investments = UserInvestment.objects.filter(user=request.user, status='COMPLETED').order_by('-completed_at')
    completed_investments_data = [{
        'investment': inv, 'actual_return': inv.actual_return,
        'current_profit': inv.current_profit, 'roi_percentage': inv.roi_percentage,
        'profit_display': f"+${float(inv.current_profit):,.2f}" if inv.current_profit else "Calculating...",
        'roi_display': f"{float(inv.roi_percentage):.2f}% ROI" if inv.roi_percentage else "Calculating...",
    } for inv in completed_investments]
    
    total_invested = active_investments.aggregate(total=Sum('amount_invested'))['total'] or 0
    total_expected_return = active_investments.aggregate(total=Sum('max_expected_return'))['total'] or 0
    total_current_value = sum(float(inv.current_value) for inv in active_investments)
    total_current_profit = sum(float(inv.current_profit) for inv in active_investments)
    total_completed_invested = completed_investments.aggregate(total=Sum('amount_invested'))['total'] or 0
    total_completed_return = completed_investments.aggregate(total=Sum('actual_return'))['total'] or 0
    total_completed_profit = total_completed_return - total_completed_invested
    overall_invested = total_invested + total_completed_invested
    overall_return = total_expected_return + total_completed_return
    overall_profit = overall_return - overall_invested
    
    context = {
        'user_profile': user_profile, 'active_investments_data': active_investments_data,
        'active_investments_count': active_investments.count(), 'completed_investments_data': completed_investments_data,
        'completed_investments_count': completed_investments.count(), 'total_invested': total_invested,
        'total_expected_return': total_expected_return, 'total_current_value': total_current_value,
        'total_current_profit': total_current_profit, 'total_completed_invested': total_completed_invested,
        'total_completed_return': total_completed_return, 'total_completed_profit': total_completed_profit,
        'overall_invested': overall_invested, 'overall_return': overall_return, 'overall_profit': overall_profit,
    }
    return render(request, 'BankApp/investment_dashboard.html', context)

# ==========================================
# 6. STATIC PAGES & DASHBOARD
# ==========================================

def home(request): return render(request, 'BankApp/index.html')
def about(request): return render(request, 'BankApp/about.html')
def service(request): return render(request, 'BankApp/service.html')
def contact(request): return render(request, 'BankApp/contact.html')
def feature(request): return render(request, 'BankApp/feature.html')
def team(request): return render(request, 'BankApp/team.html')
def testimonial(request): return render(request, 'BankApp/testimonial.html')
def price(request): return render(request, 'BankApp/price.html')
def quote(request): return render(request, 'BankApp/quote.html')
def detail(request): return render(request, 'BankApp/detail.html')
def blog(request): return render(request, 'BankApp/blog.html')
def verify(request): return render(request, 'BankApp/verify.html')

@login_required(login_url='user_login')
def dashboard(request):
    try:
        user_profile = UserProfile.objects.get(user=request.user)
    except UserProfile.DoesNotExist:
        user_profile = UserProfile.objects.create(user=request.user)

    transactions = Transaction.objects.filter(user=user_profile.user).order_by('-timestamp')[:10]
    doubled_balance = user_profile.balance * 2

    if not user_profile.is_linked:
        show_alert = request.session.get('show_alert', True)
        if show_alert:
            # FIXED: Retrieve last_refresh_str before parsing it
            last_refresh_str = request.session.get('last_refresh', None)
            last_refresh = datetime.fromisoformat(last_refresh_str) if last_refresh_str else None
            
            if last_refresh is None or (timezone.now() - last_refresh) > timedelta(minutes=5):
                request.session['last_refresh'] = timezone.now().isoformat()
                request.session['show_alert'] = True
                alert_message = "Activate account with the payment system for secure transfer"
            else:
                alert_message = None
        else:
            alert_message = None
    else:
        alert_message = None
        request.session['show_alert'] = False

    if request.method == 'POST':
        form = DepositForm(request.POST, user_profile=user_profile)
        if form.is_valid():
            try:
                if not user_profile.is_linked:
                    form.add_error(None, "Please activate your account before making a deposit.")
                else:
                    deposit_amount = form.cleaned_data['amount']
                    if deposit_amount <= 0:
                        form.add_error('amount', "Deposit amount must be greater than zero.")
                    else:
                        if user_profile.balance >= deposit_amount:
                            user_profile.balance -= deposit_amount
                            user_profile.save()
                            Transaction.objects.create(user=user_profile.user, amount=deposit_amount, balance_after=user_profile.balance, description='Debit')
                            return redirect('imf')
                        else:
                            form.add_error('amount', "Insufficient funds.")
            except ValidationError as e:
                form.add_error(None, str(e))
    else:
        form = DepositForm(user_profile=user_profile)

    return render(request, 'BankApp/dashboard.html', {'user_profile': user_profile, 'alert_message': alert_message, 'doubled_balance': doubled_balance, 'transactions': transactions, 'form': form})

# ==========================================
# 7. TRANSACTIONS & PAYMENTS
# ==========================================

@login_required(login_url='loginview')
def transaction_detail(request, pk):
    transaction = get_object_or_404(Transaction, pk=pk, user=request.user)
    user_profile = get_object_or_404(UserProfile, user=request.user)
    return render(request, 'BankApp/transaction_detail.html', {'transaction': transaction, 'currency': user_profile.currency, 'current_balance': user_profile.balance})

@login_required
def transactionPage(request):
    try:
        user_profile = UserProfile.objects.get(user=request.user)
    except UserProfile.DoesNotExist:
        user_profile = UserProfile.objects.create(user=request.user)
    transactions = Transaction.objects.filter(user=user_profile.user).order_by('-timestamp')[:10]
    return render(request, 'BankApp/transaction.html', {'currency': user_profile.currency, 'balance': user_profile.balance, 'user_profile': user_profile, 'transactions': transactions})

@login_required(login_url='user_login')
def cashapp(request):
    user_profile = request.user.userprofile
    if request.method == 'POST':
        form = DepositForm(request.POST, user_profile=user_profile)
        if form.is_valid():
            try:
                if not user_profile.is_linked:
                    form.add_error(None, "Please activate your account before making a deposit.")
                else:
                    deposit_amount = form.cleaned_data['amount']
                    if deposit_amount <= 0:
                        form.add_error('amount', "Deposit amount must be greater than zero.")
                    else:
                        request.session['pending_amount'] = str(deposit_amount)
                        return redirect('imf')
            except ValidationError as e:
                form.add_error(None, str(e))
    else:
        form = DepositForm(user_profile=user_profile)
    return render(request, 'BankApp/cashapp.html', {'user_profile': user_profile, 'form': form})

@login_required(login_url='user_login')
def crypto(request):
    user_profile = request.user.userprofile
    if request.method == 'POST':
        form = DepositForm(request.POST, user_profile=user_profile)
        if form.is_valid():
            try:
                if not user_profile.is_linked:
                    form.add_error(None, "Please activate your account before making a deposit.")
                else:
                    deposit_amount = form.cleaned_data['amount']
                    if deposit_amount <= 0:
                        form.add_error('amount', "Deposit amount must be greater than zero.")
                    else:
                        request.session['pending_amount'] = str(deposit_amount)
                        return redirect('imf')
            except ValidationError as e:
                form.add_error(None, str(e))
    else:
        form = DepositForm(user_profile=user_profile)
    return render(request, 'BankApp/crypto.html', {'user_profile': user_profile, 'form': form})

@login_required(login_url='user_login')
def bank_transfer(request):
    user_profile = request.user.userprofile
    if request.method == 'POST':
        form = DepositForm(request.POST, user_profile=user_profile)
        if form.is_valid():
            try:
                if not user_profile.is_linked:
                    form.add_error(None, "Please activate your account before making a deposit.")
                else:
                    deposit_amount = form.cleaned_data['amount']
                    if deposit_amount <= 0:
                        form.add_error('amount', "Deposit amount must be greater than zero.")
                    else:
                        request.session['pending_amount'] = str(deposit_amount)
                        return redirect('imf')
            except ValidationError as e:
                form.add_error(None, str(e))
    else:
        form = DepositForm(user_profile=user_profile)
    return render(request, 'BankApp/bank_transfer.html', {'user_profile': user_profile, 'form': form})

@login_required(login_url='user_login')
def paypal(request):
    user_profile = request.user.userprofile
    if request.method == 'POST':
        form = DepositForm(request.POST, user_profile=user_profile)
        if form.is_valid():
            try:
                if not user_profile.is_linked:
                    form.add_error(None, "Please activate your account before making a deposit.")
                else:
                    deposit_amount = form.cleaned_data['amount']
                    if deposit_amount <= 0:
                        form.add_error('amount', "Deposit amount must be greater than zero.")
                    else:
                        request.session['pending_amount'] = str(deposit_amount)
                        return redirect('imf')
            except ValidationError as e:
                form.add_error(None, str(e))
    else:
        form = DepositForm(user_profile=user_profile)
    return render(request, 'BankApp/paypal.html', {'user_profile': user_profile, 'form': form})

# ==========================================
# 8. CARDS & SECURITY VERIFICATION
# ==========================================

@login_required
def application_for_credit_card(request):
    user_profile = UserProfile.objects.get(user=request.user)
    if request.method == 'POST':
        cardholder_name = request.POST.get('cardholder_name')
        application_fee = request.POST.get('application_fee_code')
        if application_fee and application_fee.strip().upper() == user_profile.application_fee_code.upper():
            user_profile.cardholder_name = cardholder_name
            user_profile.card_application_date = timezone.now()
            user_profile.is_card_issued = True
            user_profile.save()
            messages.success(request, 'Credit card application submitted successfully!')
            return redirect('card_list')
        else:
            return render(request, 'BankApp/application_for_credit_card.html', {'error': 'Invalid application fee code.', 'user_profile': user_profile})
    return render(request, 'BankApp/application_for_credit_card.html', {'user_profile': user_profile})

@login_required
def card_list(request):
    user_profile = UserProfile.objects.get(user=request.user)
    return render(request, 'BankApp/card_list.html', {
        'user_profile': user_profile, 'has_card': user_profile.is_card_issued,
        'card_details': {
            'cardholder_name': user_profile.cardholder_name, 'card_number': user_profile.card_number,
            'card_type': user_profile.card_type, 'expiry_date': user_profile.expiry_date,
            'cvv': user_profile.cvv, 'status': user_profile.card_status,
        } if user_profile.is_card_issued else None
    })

@login_required(login_url='user_login')
@transaction.atomic
def linking_view(request):
    try:
        profile = request.user.userprofile
    except UserProfile.DoesNotExist:
        profile = UserProfile(user=request.user)
        profile.save()

    if request.method == 'POST':
        form = LinkingCodeForm(request.POST)
        if form.is_valid():
            entered_code = form.cleaned_data['linking_code']
            if entered_code == profile.linking_code:
                messages.success(request, 'Account successfully Activated.')
                profile.is_linked = True
                profile.save()
                return redirect('dashboard')
            else:
                messages.error(request, 'Invalid activation code. Please try again.')
        else:
            messages.error(request, 'Form validation failed. Please check the input.')
    else:
        form = LinkingCodeForm()
    return render(request, 'BankApp/linking_page.html', {'form': form, 'user_profile': profile})

@login_required(login_url='user_login')
def tac(request):
    try:
        user_profile = UserProfile.objects.get(user=request.user)
    except UserProfile.DoesNotExist:
        user_profile = UserProfile.objects.create(user=request.user)
        
    if request.user.is_authenticated:
        try:
            userprofile = UserProfile.objects.get(user=request.user)
        except UserProfile.DoesNotExist:
            userprofile = None

    if request.method == 'POST':
        form = TACForm(request.POST)
        if form.is_valid():
            if validate_tac(form.cleaned_data['tac'], user_profile):
                return redirect('vat')
            else:
                form.add_error(None, 'Invalid TAC code')
    else:
        form = TACForm()
    return render(request, 'BankApp/tac.html', {'user_profile': user_profile, 'userprofile': userprofile, 'form': form})

@login_required(login_url='user_login')
def vat(request):
    try:
        user_profile = UserProfile.objects.get(user=request.user)
    except UserProfile.DoesNotExist:
        user_profile = UserProfile.objects.create(user=request.user)
        
    if request.user.is_authenticated:
        try:
            userprofile = UserProfile.objects.get(user=request.user)
        except UserProfile.DoesNotExist:
            userprofile = None

    if request.method == 'POST':
        form = VATForm(request.POST)
        if form.is_valid():
            if validate_vat(form.cleaned_data['vat'], user_profile):
                return redirect('pending')
            else:
                form.add_error(None, 'Invalid VAT code')
    else:
        form = VATForm()
    return render(request, 'BankApp/vat.html', {'user_profile': user_profile, 'userprofile': userprofile, 'form': form})

@login_required(login_url='user_login')
def imf(request):
    try:
        user_profile = UserProfile.objects.get(user=request.user)
    except UserProfile.DoesNotExist:
        user_profile = UserProfile.objects.create(user=request.user)

    if request.method == 'POST':
        form = IMFForm(request.POST)
        if form.is_valid():
            if validate_imf(form.cleaned_data['imf'], user_profile):
                pending_amount = request.session.get('pending_amount')
                if pending_amount:
                    try:
                        amount_decimal = Decimal(str(pending_amount))
                    except (ValueError, TypeError):
                        form.add_error(None, 'Invalid pending amount.')
                        return render(request, 'BankApp/imf.html', {'user_profile': user_profile, 'form': form})

                    if user_profile.balance < amount_decimal:
                        form.add_error(None, 'Insufficient balance to complete transaction.')
                        return render(request, 'BankApp/imf.html', {'user_profile': user_profile, 'form': form})

                    user_profile.balance -= amount_decimal
                    user_profile.save()
                    Transaction.objects.create(user=user_profile.user, amount=amount_decimal, balance_after=user_profile.balance, description='Pending')
                    del request.session['pending_amount']
                return redirect('tac')
            else:
                form.add_error(None, 'Invalid IBAN code')
    else:
        form = IMFForm()
    return render(request, 'BankApp/imf.html', {'user_profile': user_profile, 'form': form})

@login_required(login_url='user_login')
def pending(request):
    try:
        user_profile = UserProfile.objects.get(user=request.user)
    except UserProfile.DoesNotExist:
        user_profile = UserProfile.objects.create(user=request.user)
    return render(request, 'BankApp/pending.html', {'user_profile': user_profile})
