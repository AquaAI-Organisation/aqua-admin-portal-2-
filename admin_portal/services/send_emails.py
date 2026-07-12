import logging
from django.core.mail import EmailMultiAlternatives, get_connection
from django.template.loader import render_to_string
from django.utils.html import strip_tags
from django.conf import settings
from datetime import datetime

logger = logging.getLogger("emails")


def send_breeder_approval_email(profile, user, activation_link=None):
    """
    Send approval email to breeder when application is approved.
    
    Args:
        profile: ExternalBreederProfile instance
        user: User instance
        activation_link: Optional activation/registration link
    """
    try:
        context = {
            "username": user.name or user.username,
            "company_name": profile.company_name or "",
            "activation_link": activation_link or f"{settings.FRONTEND_HOST}/breeder/{profile.id}/payments/",
            "app_name": "AquaAI",
            "year": datetime.now().year,
            "support_email": "providers@aquaai.uk",
            "dashboard_url": f"{settings.FRONTEND_HOST}/dashboard",
            "entity_type": "breeder",
        }
        
        html_message = render_to_string("emails/breeders/breeder_approval.html", context)
        plain_message = strip_tags(html_message)
        
        subject = "Congratulations — your Aqua Providers application is approved"
        
        connection = get_connection()
        if connection.connection is None:
            connection.open()
        
        email = EmailMultiAlternatives(
            subject=subject,
            body=plain_message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[user.email],
            connection=connection,
        )
        email.attach_alternative(html_message, "text/html")
        email.send(fail_silently=False)
        
        if connection.connection:
            connection.close()
        
        logger.info(f"Breeder approval email sent to {user.email}")
        return True
        
    except Exception as e:
        logger.error(f"Error sending breeder approval email: {str(e)}", exc_info=True)
        return False


def send_breeder_rejection_email(profile, user):
    """
    Send rejection email to breeder when application is rejected.
    
    Args:
        profile: ExternalBreederProfile instance
        user: User instance
    """
    try:
        context = {
            "username": user.name or user.username,
            "company_name": profile.company_name or "",
            "app_name": "AquaAI",
            "year": datetime.now().year,
            "support_email": "providers@aquaai.uk",
            "entity_type": "breeder",
        }
        
        html_message = render_to_string("emails/breeders/breeder_rejection.html", context)
        plain_message = strip_tags(html_message)
        
        subject = "Your Aqua Providers application"
        
        connection = get_connection()
        if connection.connection is None:
            connection.open()
        
        email = EmailMultiAlternatives(
            subject=subject,
            body=plain_message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[user.email],
            connection=connection,
        )
        email.attach_alternative(html_message, "text/html")
        email.send(fail_silently=False)
        
        if connection.connection:
            connection.close()
        
        logger.info(f"Breeder rejection email sent to {user.email}")
        return True
        
    except Exception as e:
        logger.error(f"Error sending breeder rejection email: {str(e)}", exc_info=True)
        return False


def send_consultant_approval_email(profile, user):
    """
    Send approval email to consultant when application is approved.
    No activation link needed - consultants just need to sign in.
    
    Args:
        profile: ExternalConsultantProfile instance
        user: User instance
    """
    try:
        context = {
            "username": user.name or user.username,
            "company_name": profile.company_name or "",
            "app_name": "AquaAI",
            "year": datetime.now().year,
            "support_email": "providers@aquaai.uk",
            "dashboard_url": f"{settings.FRONTEND_HOST}/consultant-dashboard",
            "entity_type": "consultant",
        }
        
        html_message = render_to_string("emails/consultants/consultant_approval.html", context)
        plain_message = strip_tags(html_message)
        
        subject = "Congratulations — your Aqua Providers application is approved"
        
        email = EmailMultiAlternatives(
            subject=subject,
            body=plain_message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[user.email],
        )
        email.attach_alternative(html_message, "text/html")
        email.send(fail_silently=False)
        
        logger.info(f"Consultant approval email sent to {user.email}")
        return True
        
    except Exception as e:
        logger.error(f"Error sending consultant approval email: {str(e)}", exc_info=True)
        return False


def send_consultant_rejection_email(profile, user):
    """
    Send rejection email to consultant when application is rejected.
    
    Args:
        profile: ExternalConsultantProfile instance
        user: User instance
    """
    try:
        context = {
            "username": user.name or user.username,
            "company_name": profile.company_name or "",
            "app_name": "AquaAI",
            "year": datetime.now().year,
            "support_email": "providers@aquaai.uk",
            "entity_type": "consultant",
        }
        
        html_message = render_to_string("emails/consultants/consultant_rejection.html", context)
        plain_message = strip_tags(html_message)
        
        subject = "Your Aqua Providers application"
        
        email = EmailMultiAlternatives(
            subject=subject,
            body=plain_message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[user.email],
        )
        email.attach_alternative(html_message, "text/html")
        email.send(fail_silently=False)
        
        logger.info(f"Consultant rejection email sent to {user.email}")
        return True
        
    except Exception as e:
        logger.error(f"Error sending consultant rejection email: {str(e)}", exc_info=True)
        return False 
    
    