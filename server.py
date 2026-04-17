"""
ManagerPresence - Serveur de Licences + Stripe
Déployé sur Render.com
Stockage persistant via Firebase Firestore
Paiements via Stripe
"""

from flask import Flask, request, jsonify, redirect
from flask_cors import CORS
from datetime import datetime, timedelta
import os
import json
import firebase_admin
from firebase_admin import credentials, firestore
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import secrets
import string
import stripe
import threading
import hashlib
import urllib.parse
import time
import requests as http_requests

app = Flask(__name__)
CORS(app)

# ============================================================
# CONFIGURATION
# ============================================================

# Token admin (à définir dans les variables d'environnement Render)
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "dev_token_change_me")

# Email pour notifications (à définir dans les variables d'environnement)
SMTP_EMAIL = os.environ.get("SMTP_EMAIL", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
NOTIFY_EMAIL = os.environ.get("NOTIFY_EMAIL", "")

# Brevo API (envoi emails de setup)
BREVO_API_KEY = os.environ.get("BREVO_API_KEY", "")

# Firebase Configuration
FIREBASE_CREDENTIALS = os.environ.get("FIREBASE_CREDENTIALS", "")

# Stripe Configuration
stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_PUBLIC_KEY = os.environ.get("STRIPE_PUBLIC_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

# OAuth Google — Création de structures simplifiée
GOOGLE_CLIENT_ID     = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
SERVER_BASE_URL      = os.environ.get("SERVER_BASE_URL", "https://managerpresence-server.onrender.com")

# Scopes OAuth nécessaires pour créer un projet Firebase
GOOGLE_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/cloud-platform",
    "https://www.googleapis.com/auth/firebase",
]

# Durée de validité du token de setup (24h)
SETUP_TOKEN_VALIDITY_SECONDS = 86400

# Prix Stripe (IDs des prix créés dans Stripe Dashboard)
STRIPE_PRICES = {
    "standard_monthly": os.environ.get("STRIPE_PRICE_STANDARD_MONTHLY", ""),
    "standard_yearly": os.environ.get("STRIPE_PRICE_STANDARD_YEARLY", ""),
    "premium_monthly": os.environ.get("STRIPE_PRICE_PREMIUM_MONTHLY", ""),
    "premium_yearly": os.environ.get("STRIPE_PRICE_PREMIUM_YEARLY", ""),
}

# URLs de redirection après paiement
PWA_SUCCESS_URL = os.environ.get("PWA_SUCCESS_URL", "https://managerpresence.netlify.app/paiement-reussi")
PWA_CANCEL_URL = os.environ.get("PWA_CANCEL_URL", "https://managerpresence.netlify.app/abonnement")

# Initialiser Firebase
if FIREBASE_CREDENTIALS:
    cred_dict = json.loads(FIREBASE_CREDENTIALS)
    cred = credentials.Certificate(cred_dict)
else:
    # Fallback pour dev local
    cred = credentials.Certificate("serviceAccountKey.json")

firebase_admin.initialize_app(cred)
db = firestore.client()

# ============================================================
# DÉFINITION DES PLANS (mis à jour avec limites)
# ============================================================

PLANS = {
    "trial": {
        "nom": "Essai gratuit (40 jours)",
        "duree_jours": 40,
        "fonctionnalites": ["tableau", "eleves", "creneaux", "export", "forum", "cadres_illimite", "import", "sms", "perso", "doc", "pwa", "stats", "backup_auto", "periodes", "support"],
        "max_cadres": 999,
        "max_membres": 9999,
        "max_creneaux": 9999
    },
    "standard": {
        "nom": "Standard",
        "fonctionnalites": ["tableau", "eleves", "creneaux", "forum", "email", "backup_manuel", "audit"],
        "max_cadres": 3,
        "max_membres": 25,
        "max_creneaux": 5
    },
    "premium": {
        "nom": "Premium",
        "fonctionnalites": ["tableau", "eleves", "creneaux", "export", "forum", "cadres_illimite", "import", "sms", "perso", "doc", "pwa", "stats", "backup_auto", "periodes", "support", "email", "backup_manuel", "audit"],
        "max_cadres": 999,
        "max_membres": 9999,
        "max_creneaux": 9999
    }
}

# Types de codes d'activation
CODE_TYPES = {
    "PREMIUM_PERMANENT": {"plan": "premium", "jours": 36500, "prefixe": "PRM"},
    "PREMIUM_1AN": {"plan": "premium", "jours": 365, "prefixe": "PR1"},
    "STANDARD_1AN": {"plan": "standard", "jours": 365, "prefixe": "ST1"},
    "PROLONGATION_60J": {"plan": None, "jours": 60, "prefixe": "P60"},
    "PROLONGATION_30J": {"plan": None, "jours": 30, "prefixe": "P30"},
}

# Durée de validité des codes PWA (en secondes)
PWA_CODE_VALIDITY = 600  # 10 minutes

# ============================================================
# UTILITAIRES - STOCKAGE FIREBASE
# ============================================================

def charger_licences():
    """Charge toutes les licences depuis Firestore"""
    try:
        docs = db.collection("licences").stream()
        return {doc.id: doc.to_dict() for doc in docs}
    except Exception as e:
        print(f"Erreur chargement licences: {e}")
        return {}

def sauvegarder_licence(project_id, licence):
    """Sauvegarde une licence dans Firestore"""
    try:
        db.collection("licences").document(project_id).set(licence)
        return True
    except Exception as e:
        print(f"Erreur sauvegarde licence: {e}")
        return False

def charger_licence(project_id):
    """Charge une licence spécifique"""
    try:
        doc = db.collection("licences").document(project_id).get()
        if doc.exists:
            return doc.to_dict()
        return None
    except Exception as e:
        print(f"Erreur chargement licence: {e}")
        return None

def charger_codes():
    """Charge tous les codes depuis Firestore"""
    try:
        docs = db.collection("codes").stream()
        return {doc.id: doc.to_dict() for doc in docs}
    except Exception as e:
        print(f"Erreur chargement codes: {e}")
        return {}

def sauvegarder_code(code, info):
    """Sauvegarde un code dans Firestore"""
    try:
        db.collection("codes").document(code).set(info)
        return True
    except Exception as e:
        print(f"Erreur sauvegarde code: {e}")
        return False

def charger_code(code):
    """Charge un code spécifique"""
    try:
        doc = db.collection("codes").document(code).get()
        if doc.exists:
            return doc.to_dict()
        return None
    except Exception as e:
        print(f"Erreur chargement code: {e}")
        return None

# ============================================================
# UTILITAIRES - CODES PWA
# ============================================================

def sauvegarder_pwa_code(code, data):
    """Sauvegarde un code PWA temporaire dans Firestore"""
    try:
        db.collection("pwa_codes").document(code).set(data)
        return True
    except Exception as e:
        print(f"Erreur sauvegarde code PWA: {e}")
        return False

def charger_pwa_code(code):
    """Charge un code PWA spécifique"""
    try:
        doc = db.collection("pwa_codes").document(code).get()
        if doc.exists:
            return doc.to_dict()
        return None
    except Exception as e:
        print(f"Erreur chargement code PWA: {e}")
        return None

def supprimer_pwa_code(code):
    """Supprime un code PWA après utilisation"""
    try:
        db.collection("pwa_codes").document(code).delete()
        return True
    except Exception as e:
        print(f"Erreur suppression code PWA: {e}")
        return False

def nettoyer_codes_expires():
    """Supprime les codes PWA expirés (appelé périodiquement)"""
    try:
        now = datetime.now().timestamp() * 1000  # en millisecondes
        expired = db.collection("pwa_codes").where("expiresAt", "<", now).stream()
        for doc in expired:
            doc.reference.delete()
    except Exception as e:
        print(f"Erreur nettoyage codes PWA: {e}")

# ============================================================
# UTILITAIRES - NOTIFICATIONS
# ============================================================

def envoyer_notification(sujet, message):
    """Envoie un email de notification"""
    if not SMTP_PASSWORD or not SMTP_EMAIL:
        print(f"[NOTIFICATION] {sujet}: {message}")
        return False
    
    try:
        msg = MIMEMultipart()
        msg["From"] = SMTP_EMAIL
        msg["To"] = NOTIFY_EMAIL
        msg["Subject"] = f"[ManagerPresence] {sujet}"
        msg.attach(MIMEText(message, "plain"))
        
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(SMTP_EMAIL, SMTP_PASSWORD)
            server.send_message(msg)
        return True
    except Exception as e:
        print(f"Erreur envoi email: {e}")
        return False

# ============================================================
# UTILITAIRES - CODES
# ============================================================

def generer_code(prefixe):
    """Génère un code unique au format PRE-XXXX-XXXX"""
    chars = string.ascii_uppercase + string.digits
    partie1 = ''.join(secrets.choice(chars) for _ in range(4))
    partie2 = ''.join(secrets.choice(chars) for _ in range(4))
    return f"{prefixe}-{partie1}-{partie2}"

# ============================================================
# UTILITAIRES - LICENCES
# ============================================================

def calculer_jours_restants(date_expiration_str):
    """Calcule le nombre de jours restants avant expiration"""
    try:
        date_exp = datetime.fromisoformat(date_expiration_str.replace("Z", "+00:00"))
        if date_exp.tzinfo:
            date_exp = date_exp.replace(tzinfo=None)
        delta = date_exp - datetime.now()
        return max(0, delta.days)
    except:
        return 0

def creer_licence_trial(project_id, nom_structure=""):
    """Crée une nouvelle licence d'essai"""
    maintenant = datetime.now()
    expiration = maintenant + timedelta(days=PLANS["trial"]["duree_jours"])
    
    licence = {
        "projectId": project_id,
        "nomStructure": nom_structure,
        "dateInscription": maintenant.isoformat(),
        "dateExpiration": expiration.isoformat(),
        "plan": "trial",
        "actif": True,
        "fonctionnalites": PLANS["trial"]["fonctionnalites"],
        "maxCadres": PLANS["trial"]["max_cadres"],
        "maxMembres": PLANS["trial"]["max_membres"],
        "maxCreneaux": PLANS["trial"]["max_creneaux"],
        "stripeCustomerId": None,
        "stripeSubscriptionId": None,
        "message": f"Bienvenue ! Votre essai gratuit expire dans {PLANS['trial']['duree_jours']} jours."
    }
    
    # Notification
    envoyer_notification(
        "🆕 Nouvelle inscription",
        f"Nouveau client inscrit !\n\nProject ID: {project_id}\nStructure: {nom_structure or 'Non renseigné'}\nDate: {maintenant.strftime('%d/%m/%Y %H:%M')}\nExpiration essai: {expiration.strftime('%d/%m/%Y')}"
    )
    
    return licence

def formater_licence_response(licence):
    """Formate la licence pour la réponse API"""
    jours_restants = calculer_jours_restants(licence.get("dateExpiration", ""))
    est_actif = licence.get("actif", False) and jours_restants > 0
    
    # Message selon le statut
    if not est_actif:
        message = "Votre licence a expiré. Souscrivez un abonnement pour continuer."
    elif jours_restants <= 7:
        message = f"⚠️ Votre licence expire dans {jours_restants} jour(s) !"
    elif jours_restants <= 30 and licence.get("plan") == "trial":
        message = f"Votre essai gratuit expire dans {jours_restants} jours."
    else:
        message = licence.get("message", "")
    
    plan_info = PLANS.get(licence.get("plan", "trial"), PLANS["trial"])
    
    return {
        "projectId": licence.get("projectId"),
        "nomStructure": licence.get("nomStructure", ""),
        "plan": licence.get("plan", "trial"),
        "planNom": plan_info["nom"],
        "actif": est_actif,
        "dateExpiration": licence.get("dateExpiration"),
        "joursRestants": jours_restants,
        "fonctionnalites": licence.get("fonctionnalites", plan_info["fonctionnalites"]),
        "maxCadres": licence.get("maxCadres", plan_info["max_cadres"]),
        "maxMembres": licence.get("maxMembres", plan_info.get("max_membres", 9999)),
        "maxCreneaux": licence.get("maxCreneaux", plan_info.get("max_creneaux", 9999)),
        "stripeCustomerId": licence.get("stripeCustomerId"),
        "stripeSubscriptionId": licence.get("stripeSubscriptionId"),
        "message": message
    }

# ============================================================
# ROUTES PUBLIQUES
# ============================================================

@app.route("/", methods=["GET", "HEAD"])
def index():
    """Route racine pour UptimeRobot et health checks"""
    return jsonify({
        "service": "ManagerPresence License Server",
        "status": "ok",
        "version": "2.0.2",
        "timestamp": datetime.now().isoformat()
    })

@app.route("/health", methods=["GET"])
def health():
    """Vérification que le serveur tourne"""
    return jsonify({"status": "ok", "timestamp": datetime.now().isoformat()})

@app.route("/licence/<project_id>", methods=["GET"])
def get_licence(project_id):
    """Récupère la licence d'un projet (crée un trial si inconnu)"""
    licence = charger_licence(project_id)
    
    if licence is None:
        # Nouveau client → créer licence trial
        nom_structure = request.args.get("nom", "")
        licence = creer_licence_trial(project_id, nom_structure)
        sauvegarder_licence(project_id, licence)
    
    return jsonify(formater_licence_response(licence))

@app.route("/licence/<project_id>/code", methods=["POST"])
def activer_code(project_id):
    """Active un code pour un projet"""
    data = request.get_json() or {}
    code = data.get("code", "").strip().upper()
    
    if not code:
        return jsonify({"error": "Code manquant"}), 400
    
    # Charger le code
    code_info = charger_code(code)
    
    if code_info is None:
        return jsonify({"error": "Code invalide"}), 404
    
    if code_info.get("utilise"):
        return jsonify({"error": "Code déjà utilisé"}), 400
    
    # Charger la licence
    licence = charger_licence(project_id)
    
    if licence is None:
        licence = creer_licence_trial(project_id)
    
    # Appliquer le code
    code_type = code_info.get("type")
    type_config = CODE_TYPES.get(code_type, {})
    
    if type_config.get("plan"):
        nouveau_plan = type_config["plan"]
        plan_config = PLANS[nouveau_plan]
        licence["plan"] = nouveau_plan
        licence["fonctionnalites"] = plan_config["fonctionnalites"]
        licence["maxCadres"] = plan_config["max_cadres"]
        licence["maxMembres"] = plan_config.get("max_membres", 9999)
        licence["maxCreneaux"] = plan_config.get("max_creneaux", 9999)
        licence["dateExpiration"] = (datetime.now() + timedelta(days=type_config["jours"])).isoformat()
    else:
        try:
            date_exp_actuelle = datetime.fromisoformat(licence["dateExpiration"].replace("Z", "+00:00"))
            if date_exp_actuelle.tzinfo:
                date_exp_actuelle = date_exp_actuelle.replace(tzinfo=None)
        except:
            date_exp_actuelle = datetime.now()
        
        if date_exp_actuelle < datetime.now():
            date_exp_actuelle = datetime.now()
        
        licence["dateExpiration"] = (date_exp_actuelle + timedelta(days=type_config["jours"])).isoformat()
    
    licence["actif"] = True
    licence["message"] = f"Code {code} activé avec succès !"
    
    # Marquer le code comme utilisé
    code_info["utilise"] = True
    code_info["utilise_par"] = project_id
    code_info["utilise_le"] = datetime.now().isoformat()
    
    # Sauvegarder
    sauvegarder_licence(project_id, licence)
    sauvegarder_code(code, code_info)
    
    # Notification
    envoyer_notification(
        "🎟️ Code activé",
        f"Un code a été activé !\n\nCode: {code}\nType: {code_type}\nProject ID: {project_id}\nStructure: {licence.get('nomStructure', 'N/A')}"
    )
    
    return jsonify({
        "success": True,
        "message": f"Code activé ! Vous êtes maintenant en plan {PLANS[licence['plan']]['nom']}.",
        "licence": formater_licence_response(licence)
    })

# ============================================================
# ROUTES STRIPE - PAIEMENT
# ============================================================

@app.route("/stripe/prices", methods=["GET"])
def stripe_prices():
    """Retourne les prix disponibles pour l'affichage dans la PWA"""
    return jsonify({
        "standard": {
            "monthly": {"id": STRIPE_PRICES["standard_monthly"], "price": 4.90, "currency": "eur"},
            "yearly": {"id": STRIPE_PRICES["standard_yearly"], "price": 49.90, "currency": "eur"}
        },
        "premium": {
            "monthly": {"id": STRIPE_PRICES["premium_monthly"], "price": 9.99, "currency": "eur"},
            "yearly": {"id": STRIPE_PRICES["premium_yearly"], "price": 99.99, "currency": "eur"}
        },
        "publicKey": STRIPE_PUBLIC_KEY
    })

@app.route("/stripe/checkout", methods=["POST"])
def stripe_checkout():
    data = request.get_json() or {}
    
    project_id = data.get("projectId", "").strip()
    price_id = data.get("priceId", "").strip()
    email = data.get("email", "").strip()
    nom_structure = data.get("nomStructure", "").strip()
    
    if not project_id or not price_id:
        return jsonify({"error": "projectId et priceId requis"}), 400
    
    valid_prices = list(STRIPE_PRICES.values())
    if price_id not in valid_prices:
        return jsonify({"error": "Prix invalide"}), 400
    
    licence = charger_licence(project_id)
    if licence is None:
        licence = creer_licence_trial(project_id, nom_structure)
        sauvegarder_licence(project_id, licence)
    
    try:
        customer_id = licence.get("stripeCustomerId")
        
        if not customer_id:
            customer = stripe.Customer.create(
                email=email or None,
                metadata={
                    "projectId": project_id,
                    "nomStructure": nom_structure or licence.get("nomStructure", "")
                }
            )
            customer_id = customer.id
            licence["stripeCustomerId"] = customer_id
            sauvegarder_licence(project_id, licence)
        
        checkout_session = stripe.checkout.Session.create(
            customer=customer_id,
            payment_method_types=["card"],
            line_items=[{"price": price_id, "quantity": 1}],
            mode="subscription",
            success_url=f"{PWA_SUCCESS_URL}?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=PWA_CANCEL_URL,
            metadata={"projectId": project_id},
            subscription_data={"metadata": {"projectId": project_id}},
            allow_promotion_codes=True
        )
        
        return jsonify({
            "success": True,
            "sessionId": checkout_session.id,
            "url": checkout_session.url
        })
        
    except stripe.error.StripeError as e:
        print(f"Erreur Stripe: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/stripe/portal", methods=["POST"])
def stripe_portal():
    data = request.get_json() or {}
    project_id = data.get("projectId", "").strip()
    
    if not project_id:
        return jsonify({"error": "projectId requis"}), 400
    
    licence = charger_licence(project_id)
    if not licence:
        return jsonify({"error": "Licence non trouvée"}), 404
    
    customer_id = licence.get("stripeCustomerId")
    if not customer_id:
        return jsonify({"error": "Aucun abonnement Stripe associé"}), 400
    
    try:
        portal_session = stripe.billing_portal.Session.create(
            customer=customer_id,
            return_url=PWA_CANCEL_URL
        )
        
        return jsonify({"success": True, "url": portal_session.url})
        
    except stripe.error.StripeError as e:
        print(f"Erreur Stripe Portal: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/stripe/webhook", methods=["POST"])
def stripe_webhook():
    payload = request.get_data(as_text=True)
    sig_header = request.headers.get("Stripe-Signature", "")
    
    if STRIPE_WEBHOOK_SECRET:
        try:
            event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
        except ValueError as e:
            print(f"Webhook payload invalide: {e}")
            return jsonify({"error": "Invalid payload"}), 400
        except stripe.error.SignatureVerificationError as e:
            print(f"Webhook signature invalide: {e}")
            return jsonify({"error": "Invalid signature"}), 400
    else:
        event = json.loads(payload)
    
    event_type = event.get("type", "")
    data_object = event.get("data", {}).get("object", {})
    
    print(f"[STRIPE WEBHOOK] Événement reçu: {event_type}")
    
    if event_type == "checkout.session.completed":
        session = data_object
        project_id = session.get("metadata", {}).get("projectId")
        subscription_id = session.get("subscription")
        customer_id = session.get("customer")
        if project_id and subscription_id:
            handle_subscription_created(project_id, subscription_id, customer_id)
    
    elif event_type == "customer.subscription.created":
        subscription = data_object
        project_id = subscription.get("metadata", {}).get("projectId")
        subscription_id = subscription.get("id")
        customer_id = subscription.get("customer")
        if project_id:
            handle_subscription_created(project_id, subscription_id, customer_id)
    
    elif event_type == "customer.subscription.updated":
        subscription = data_object
        project_id = subscription.get("metadata", {}).get("projectId")
        if project_id:
            handle_subscription_updated(project_id, subscription)
    
    elif event_type == "customer.subscription.deleted":
        subscription = data_object
        project_id = subscription.get("metadata", {}).get("projectId")
        if project_id:
            handle_subscription_cancelled(project_id)
    
    elif event_type == "invoice.payment_succeeded":
        invoice = data_object
        subscription_id = invoice.get("subscription")
        if subscription_id:
            try:
                subscription = stripe.Subscription.retrieve(subscription_id)
                project_id = subscription.get("metadata", {}).get("projectId")
                if project_id:
                    handle_payment_succeeded(project_id, subscription)
            except Exception as e:
                print(f"Erreur récupération subscription: {e}")
    
    elif event_type == "invoice.payment_failed":
        invoice = data_object
        subscription_id = invoice.get("subscription")
        customer_email = invoice.get("customer_email", "")
        if subscription_id:
            try:
                subscription = stripe.Subscription.retrieve(subscription_id)
                project_id = subscription.get("metadata", {}).get("projectId")
                if project_id:
                    handle_payment_failed(project_id, customer_email)
            except Exception as e:
                print(f"Erreur récupération subscription: {e}")
    
    return jsonify({"received": True})

def handle_subscription_created(project_id, subscription_id, customer_id):
    print(f"[STRIPE] Nouvel abonnement pour {project_id}: {subscription_id}")
    
    try:
        subscription = stripe.Subscription.retrieve(subscription_id)
        price_id = subscription["items"]["data"][0]["price"]["id"]
        
        if price_id in [STRIPE_PRICES["premium_monthly"], STRIPE_PRICES["premium_yearly"]]:
            nouveau_plan = "premium"
        else:
            nouveau_plan = "standard"
        
        interval = subscription["items"]["data"][0]["price"]["recurring"]["interval"]
        jours = 365 if interval == "year" else 31
        
        licence = charger_licence(project_id)
        if licence:
            plan_config = PLANS[nouveau_plan]
            licence["plan"] = nouveau_plan
            licence["fonctionnalites"] = plan_config["fonctionnalites"]
            licence["maxCadres"] = plan_config["max_cadres"]
            licence["maxMembres"] = plan_config.get("max_membres", 9999)
            licence["maxCreneaux"] = plan_config.get("max_creneaux", 9999)
            licence["dateExpiration"] = (datetime.now() + timedelta(days=jours)).isoformat()
            licence["actif"] = True
            licence["stripeCustomerId"] = customer_id
            licence["stripeSubscriptionId"] = subscription_id
            licence["message"] = f"Merci ! Votre abonnement {plan_config['nom']} est actif."
            
            sauvegarder_licence(project_id, licence)
            
            envoyer_notification(
                "💳 Nouvel abonnement Stripe",
                f"Nouvel abonnement !\n\nProject ID: {project_id}\nStructure: {licence.get('nomStructure', 'N/A')}\nPlan: {nouveau_plan}\nSubscription: {subscription_id}"
            )
    
    except Exception as e:
        print(f"Erreur handle_subscription_created: {e}")

def handle_subscription_updated(project_id, subscription):
    print(f"[STRIPE] Abonnement mis à jour pour {project_id}")
    
    try:
        price_id = subscription["items"]["data"][0]["price"]["id"]
        status = subscription.get("status")
        
        if price_id in [STRIPE_PRICES["premium_monthly"], STRIPE_PRICES["premium_yearly"]]:
            nouveau_plan = "premium"
        else:
            nouveau_plan = "standard"
        
        licence = charger_licence(project_id)
        if licence:
            if status == "active":
                plan_config = PLANS[nouveau_plan]
                licence["plan"] = nouveau_plan
                licence["fonctionnalites"] = plan_config["fonctionnalites"]
                licence["maxCadres"] = plan_config["max_cadres"]
                licence["maxMembres"] = plan_config.get("max_membres", 9999)
                licence["maxCreneaux"] = plan_config.get("max_creneaux", 9999)
                licence["actif"] = True
                
                period_end = subscription.get("current_period_end")
                if period_end:
                    licence["dateExpiration"] = datetime.fromtimestamp(period_end).isoformat()
            
            elif status in ["past_due", "unpaid"]:
                licence["message"] = "⚠️ Problème de paiement - Mettez à jour votre carte."
            
            elif status == "canceled":
                licence["message"] = "Abonnement annulé. Il reste actif jusqu'à la fin de la période."
            
            sauvegarder_licence(project_id, licence)
    
    except Exception as e:
        print(f"Erreur handle_subscription_updated: {e}")

def handle_subscription_cancelled(project_id):
    print(f"[STRIPE] Abonnement annulé pour {project_id}")
    
    licence = charger_licence(project_id)
    if licence:
        licence["stripeSubscriptionId"] = None
        licence["message"] = "Votre abonnement a été annulé. Accès jusqu'au " + licence.get("dateExpiration", "")[:10]
        sauvegarder_licence(project_id, licence)
        
        envoyer_notification(
            "❌ Abonnement annulé",
            f"Abonnement annulé !\n\nProject ID: {project_id}\nStructure: {licence.get('nomStructure', 'N/A')}"
        )

def handle_payment_succeeded(project_id, subscription):
    print(f"[STRIPE] Paiement réussi pour {project_id}")
    
    licence = charger_licence(project_id)
    if licence:
        period_end = subscription.get("current_period_end")
        if period_end:
            licence["dateExpiration"] = datetime.fromtimestamp(period_end).isoformat()
        licence["actif"] = True
        licence["message"] = "Merci ! Votre abonnement a été renouvelé."
        sauvegarder_licence(project_id, licence)

def handle_payment_failed(project_id, customer_email):
    print(f"[STRIPE] Paiement échoué pour {project_id}")
    
    licence = charger_licence(project_id)
    if licence:
        licence["message"] = "⚠️ Échec du paiement. Mettez à jour votre carte via le portail client."
        sauvegarder_licence(project_id, licence)
        
        envoyer_notification(
            "⚠️ Paiement échoué",
            f"Échec de paiement !\n\nProject ID: {project_id}\nStructure: {licence.get('nomStructure', 'N/A')}\nEmail: {customer_email}"
        )

# ============================================================
# ROUTES PWA - Accès sécurisé temporaire
# ============================================================

@app.route("/pwa/generate", methods=["POST"])
def pwa_generate():
    data = request.get_json() or {}
    
    required_fields = ["projectId", "code", "firebaseConfig"]
    for field in required_fields:
        if not data.get(field):
            return jsonify({"error": f"Champ manquant: {field}"}), 400
    
    project_id = data["projectId"]
    code = data["code"].upper()
    generated_by = data.get("generatedBy", "Admin")
    club_name = data.get("clubName", "")
    firebase_config = data["firebaseConfig"]
    
    licence = charger_licence(project_id)
    if licence:
        plan = licence.get("plan", "trial")
        if plan == "standard":
            return jsonify({"error": "L'accès PWA nécessite une licence Trial ou Premium"}), 403
        
        jours_restants = calculer_jours_restants(licence.get("dateExpiration", ""))
        if jours_restants <= 0:
            return jsonify({"error": "Licence expirée"}), 403
    
    now = datetime.now()
    expires_at = now + timedelta(seconds=PWA_CODE_VALIDITY)
    expires_at_ms = int(expires_at.timestamp() * 1000)
    
    pwa_data = {
        "projectId": project_id,
        "code": code,
        "generatedBy": generated_by,
        "clubName": club_name,
        "firebaseConfig": firebase_config,
        "createdAt": now.isoformat(),
        "expiresAt": expires_at_ms,
        "used": False
    }
    
    if not sauvegarder_pwa_code(code, pwa_data):
        return jsonify({"error": "Erreur serveur lors de la sauvegarde"}), 500
    
    nettoyer_codes_expires()
    
    return jsonify({
        "success": True,
        "code": code,
        "expiresAt": expires_at_ms,
        "validitySeconds": PWA_CODE_VALIDITY
    }), 201


@app.route("/pwa/verify", methods=["POST"])
def pwa_verify():
    data = request.get_json() or {}
    code = data.get("code", "").strip().upper()
    
    if not code:
        return jsonify({"error": "Code manquant"}), 400
    
    pwa_data = charger_pwa_code(code)
    
    if pwa_data is None:
        return jsonify({"error": "Code invalide ou expiré"}), 404
    
    now_ms = int(datetime.now().timestamp() * 1000)
    expires_at = pwa_data.get("expiresAt", 0)
    
    if now_ms > expires_at:
        supprimer_pwa_code(code)
        return jsonify({"error": "Code expiré"}), 410
    
    if pwa_data.get("used", False):
        return jsonify({"error": "Code déjà utilisé"}), 400
    
    pwa_data["used"] = True
    pwa_data["usedAt"] = datetime.now().isoformat()
    sauvegarder_pwa_code(code, pwa_data)
    
    project_id = pwa_data.get("projectId", "")
    licence = charger_licence(project_id)
    licence_info = formater_licence_response(licence) if licence else None
    
    return jsonify({
        "success": True,
        "projectId": project_id,
        "clubName": pwa_data.get("clubName", ""),
        "firebaseConfig": pwa_data.get("firebaseConfig", {}),
        "generatedBy": pwa_data.get("generatedBy", ""),
        "licence": licence_info
    })


@app.route("/pwa/status/<code>", methods=["GET"])
def pwa_status(code):
    code = code.upper()
    pwa_data = charger_pwa_code(code)
    
    if pwa_data is None:
        return jsonify({"exists": False, "status": "not_found"})
    
    now_ms = int(datetime.now().timestamp() * 1000)
    expires_at = pwa_data.get("expiresAt", 0)
    
    if now_ms > expires_at:
        return jsonify({"exists": True, "status": "expired"})
    
    if pwa_data.get("used", False):
        return jsonify({
            "exists": True,
            "status": "used",
            "usedAt": pwa_data.get("usedAt", "")
        })
    
    remaining_seconds = int((expires_at - now_ms) / 1000)
    return jsonify({
        "exists": True,
        "status": "active",
        "remainingSeconds": remaining_seconds
    })

# ============================================================
# ROUTES ADMIN (protégées par token)
# ============================================================

def verifier_admin():
    """Vérifie le token admin dans les headers"""
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    return token == ADMIN_TOKEN

@app.route("/admin/liste", methods=["GET"])
def admin_liste():
    if not verifier_admin():
        return jsonify({"error": "Non autorisé"}), 401
    
    licences = charger_licences()
    liste = [formater_licence_response(l) for l in licences.values()]
    liste.sort(key=lambda x: x.get("dateExpiration", ""), reverse=True)
    
    return jsonify({"total": len(liste), "licences": liste})

@app.route("/admin/gencode", methods=["POST"])
def admin_gencode():
    if not verifier_admin():
        return jsonify({"error": "Non autorisé"}), 401
    
    data = request.get_json() or {}
    code_type = data.get("type", "").upper()
    
    if code_type not in CODE_TYPES:
        return jsonify({"error": f"Type invalide. Types: {list(CODE_TYPES.keys())}"}), 400
    
    config = CODE_TYPES[code_type]
    codes = charger_codes()
    
    nouveau_code = generer_code(config["prefixe"])
    while nouveau_code in codes:
        nouveau_code = generer_code(config["prefixe"])
    
    code_info = {
        "type": code_type,
        "cree_le": datetime.now().isoformat(),
        "utilise": False
    }
    
    sauvegarder_code(nouveau_code, code_info)
    
    return jsonify({
        "code": nouveau_code,
        "type": code_type,
        "effet": f"{config.get('plan', 'Prolongation')} - {config['jours']} jours"
    })

@app.route("/admin/codes", methods=["GET"])
def admin_codes():
    if not verifier_admin():
        return jsonify({"error": "Non autorisé"}), 401
    
    codes = charger_codes()
    liste = [{"code": c, **info} for c, info in codes.items()]
    liste.sort(key=lambda x: x.get("cree_le", ""), reverse=True)
    
    return jsonify({"total": len(liste), "codes": liste})

@app.route("/admin/pwa-codes", methods=["GET"])
def admin_pwa_codes():
    if not verifier_admin():
        return jsonify({"error": "Non autorisé"}), 401
    
    try:
        docs = db.collection("pwa_codes").stream()
        codes = []
        now_ms = int(datetime.now().timestamp() * 1000)
        
        for doc in docs:
            data = doc.to_dict()
            expires_at = data.get("expiresAt", 0)
            status = "expired" if now_ms > expires_at else ("used" if data.get("used") else "active")
            codes.append({
                "code": doc.id,
                "projectId": data.get("projectId", ""),
                "clubName": data.get("clubName", ""),
                "generatedBy": data.get("generatedBy", ""),
                "createdAt": data.get("createdAt", ""),
                "status": status
            })
        
        codes.sort(key=lambda x: x.get("createdAt", ""), reverse=True)
        return jsonify({"total": len(codes), "codes": codes})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/licence/<project_id>", methods=["POST"])
def admin_update_licence(project_id):
    if not verifier_admin():
        return jsonify({"error": "Non autorisé"}), 401
    
    data = request.get_json() or {}
    licence = charger_licence(project_id)
    
    if licence is None:
        return jsonify({"error": "Licence non trouvée"}), 404
    
    if "plan" in data and data["plan"] in PLANS:
        nouveau_plan = data["plan"]
        plan_config = PLANS[nouveau_plan]
        licence["plan"] = nouveau_plan
        licence["fonctionnalites"] = plan_config["fonctionnalites"]
        licence["maxCadres"] = plan_config["max_cadres"]
    
    if "actif" in data:
        licence["actif"] = bool(data["actif"])
    
    if "dateExpiration" in data:
        licence["dateExpiration"] = data["dateExpiration"]
    
    if "joursSupplementaires" in data:
        try:
            date_exp = datetime.fromisoformat(licence["dateExpiration"].replace("Z", "+00:00"))
            if date_exp.tzinfo:
                date_exp = date_exp.replace(tzinfo=None)
        except:
            date_exp = datetime.now()
        
        if date_exp < datetime.now():
            date_exp = datetime.now()
        
        licence["dateExpiration"] = (date_exp + timedelta(days=int(data["joursSupplementaires"]))).isoformat()
    
    if "nomStructure" in data:
        licence["nomStructure"] = data["nomStructure"]
    
    if "message" in data:
        licence["message"] = data["message"]
    
    sauvegarder_licence(project_id, licence)
    
    return jsonify({"success": True, "licence": formater_licence_response(licence)})


@app.route("/admin/licence/<project_id>", methods=["PUT"])
def admin_edit_licence(project_id):
    if not verifier_admin():
        return jsonify({"error": "Non autorisé"}), 401
    
    data = request.get_json() or {}
    licence = charger_licence(project_id)
    
    if licence is None:
        return jsonify({"error": "Licence non trouvée"}), 404
    
    if "plan" in data and data["plan"] in PLANS:
        nouveau_plan = data["plan"]
        plan_config = PLANS[nouveau_plan]
        licence["plan"] = nouveau_plan
        licence["fonctionnalites"] = plan_config["fonctionnalites"]
        if "maxCadres" not in data:
            licence["maxCadres"] = plan_config["max_cadres"]
    
    if "duree" in data:
        duree_jours = int(data["duree"])
        licence["dateExpiration"] = (datetime.now() + timedelta(days=duree_jours)).isoformat()
        licence["actif"] = True
    
    if "maxCadres" in data:
        licence["maxCadres"] = int(data["maxCadres"])
    
    if "nomStructure" in data:
        licence["nomStructure"] = data["nomStructure"]
    
    sauvegarder_licence(project_id, licence)
    
    print(f"[ADMIN] Licence modifiée: {project_id} -> plan={licence.get('plan')}, expiration={licence.get('dateExpiration')}")
    
    return jsonify({"success": True, "licence": formater_licence_response(licence)})


# ============================================================
# UTILITAIRES — CRÉATION DE STRUCTURE (mode simple)
# ============================================================

def sauvegarder_setup(token, data):
    try:
        db.collection("setup_sessions").document(token).set(data)
        return True
    except Exception as e:
        print(f"Erreur sauvegarde setup: {e}")
        return False

def charger_setup(token):
    try:
        doc = db.collection("setup_sessions").document(token).get()
        if doc.exists:
            return doc.to_dict()
        return None
    except Exception as e:
        print(f"Erreur chargement setup: {e}")
        return None

def supprimer_setup(token):
    try:
        db.collection("setup_sessions").document(token).delete()
    except Exception as e:
        print(f"Erreur suppression setup: {e}")

def generer_token_setup():
    return secrets.token_urlsafe(32)

def envoyer_email_setup(gmail, club_name, setup_url):
    """Envoie l'email de setup via Brevo API"""
    if not BREVO_API_KEY:
        print(f"[SETUP EMAIL] BREVO_API_KEY manquant — URL: {setup_url}")
        return True
    try:
        html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;padding:20px">
  <h1 style="color:#1565C0;text-align:center">🏔️ ManagerPresence</h1>
  <h2>Votre espace est presque prêt !</h2>
  <p style="color:#555;font-size:16px">
    La structure <strong>"{club_name}"</strong> a été initialisée.<br>
    Il ne reste qu'une étape : vous connecter avec votre compte Google.
  </p>
  <div style="text-align:center;margin:30px 0">
    <a href="{setup_url}"
       style="background:#1565C0;color:white;padding:16px 32px;
              text-decoration:none;border-radius:8px;font-size:16px;
              font-weight:bold;display:inline-block">
      Finaliser la création →
    </a>
  </div>
  <p style="color:#888;font-size:13px;text-align:center">
    Ce lien est valable 24 heures.<br>
    Si vous n'êtes pas à l'origine de cette demande, ignorez cet email.
  </p>
  <hr style="border:none;border-top:1px solid #eee;margin:20px 0">
  <p style="color:#aaa;font-size:12px;text-align:center">
    ManagerPresence — Données hébergées en France (Firebase europe-west9)
  </p>
</body></html>"""

        import urllib.request
        payload = json.dumps({
            "sender": {"name": "ManagerPresence", "email": "cp.support.dev@gmail.com"},
            "to": [{"email": gmail}],
            "subject": f"Créez votre espace {club_name} — ManagerPresence",
            "htmlContent": html
        }).encode("utf-8")
        req = urllib.request.Request(
            "https://api.brevo.com/v3/smtp/email",
            data=payload,
            headers={
                "api-key": BREVO_API_KEY,
                "Content-Type": "application/json",
                "Accept": "application/json"
            },
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode())
            print(f"[SETUP] Email Brevo envoyé à {gmail} — id: {result.get('messageId')}")
        return True
    except Exception as e:
        print(f"[SETUP] Erreur envoi email Brevo: {e}")
        return False

def envoyer_email_confirmation(gmail, club_name, su_password):
    """Envoie l'email de confirmation avec le mot de passe SU via Brevo"""
    if not BREVO_API_KEY:
        print(f"[CONFIRMATION] BREVO_API_KEY manquant — MDP: {su_password}")
        return True
    try:
        import urllib.request
        html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;padding:20px">
  <h1 style="color:#1565C0;text-align:center">🏔️ ManagerPresence</h1>
  <div style="background:#E8F5E9;border-radius:8px;padding:20px;margin-bottom:20px">
    <h2 style="color:#2E7D32;margin:0">✅ Votre espace est opérationnel !</h2>
  </div>
  <p style="color:#555;font-size:16px">
    La structure <strong>"{club_name}"</strong> est prête.
  </p>
  <div style="background:#FFF3E0;border-radius:8px;padding:20px;margin:20px 0;
              border-left:4px solid #E65100">
    <h3 style="color:#E65100;margin-top:0">🔐 Votre mot de passe Super Utilisateur</h3>
    <div style="background:white;border-radius:4px;padding:12px;text-align:center;
                font-family:monospace;font-size:22px;font-weight:bold;
                color:#E65100;letter-spacing:2px">
      {su_password}
    </div>
    <p style="color:#BF360C;font-size:13px;margin-bottom:0">
      ⚠️ <strong>Conservez ce mot de passe précieusement.</strong><br>
      Il ne peut pas être récupéré.
    </p>
  </div>
  <h3>Comment accéder à votre espace ?</h3>
  <ol style="color:#555;font-size:15px;line-height:1.8">
    <li>Ouvrez l'application <strong>ManagerPresence</strong></li>
    <li>Votre structure <strong>"{club_name}"</strong> apparaît automatiquement</li>
    <li>Utilisez le mot de passe SU ci-dessus pour l'administration</li>
  </ol>
  <hr style="border:none;border-top:1px solid #eee;margin:20px 0">
  <p style="color:#aaa;font-size:12px;text-align:center">
    ManagerPresence — Données hébergées en France (Firebase europe-west9)
  </p>
</body></html>"""
        payload = json.dumps({
            "sender": {"name": "ManagerPresence", "email": "cp.support.dev@gmail.com"},
            "to": [{"email": gmail}],
            "subject": f"✅ Votre espace {club_name} est opérationnel !",
            "htmlContent": html
        }).encode("utf-8")
        req = urllib.request.Request(
            "https://api.brevo.com/v3/smtp/email",
            data=payload,
            headers={
                "api-key": BREVO_API_KEY,
                "Content-Type": "application/json",
                "Accept": "application/json"
            },
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode())
            print(f"[CONFIRMATION] Email Brevo envoyé à {gmail} — id: {result.get('messageId')}")
        return True
    except Exception as e:
        print(f"[CONFIRMATION] Erreur envoi email Brevo: {e}")
        return False


def _configure_firebase_logic(token, session):
    """
    Crée le projet Firebase de A à Z sur le compte Google de l'utilisateur,
    puis configure Firestore, l'app Android, et récupère l'API key.
    Tout est automatique — l'utilisateur n'a jamais besoin d'ouvrir Firebase Console.

    FIXES v2.0.2 :
    - Délai augmenté après addFirebase() : 30s au lieu de 8s
      → Firebase Auth n'est pas disponible immédiatement après activation
    - Auth Email/Password activée en plus de l'anonyme
    - Suppression de l'envoi d'email confirmation ici
      → L'email est envoyé uniquement depuis setup_finalize() avec le vrai mot de passe
    """
    club_name = session.get("club_name", "")
    gmail = session.get("gmail", "")
    token_data = session.get("token_data", {})

    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        creds = Credentials(
            token=token_data.get("access_token", ""),
            refresh_token=token_data.get("refresh_token"),
            token_uri="https://oauth2.googleapis.com/token",
            client_id=GOOGLE_CLIENT_ID,
            client_secret=GOOGLE_CLIENT_SECRET,
            scopes=GOOGLE_SCOPES
        )

        # === ÉTAPE 1 : Créer le projet Google Cloud ===
        sauvegarder_setup(token, {**session, "status": "creating_project"})
        suffix = secrets.token_hex(4)
        safe_name = "".join(c.lower() if c.isalnum() else "-" for c in club_name)[:20].strip("-")
        project_id = f"mp-{safe_name}-{suffix}"
        print(f"[CONFIGURE] 🔨 Création projet GCloud: {project_id}")

        crm = build("cloudresourcemanager", "v1", credentials=creds)
        crm.projects().create(body={
            "projectId": project_id,
            "name": club_name
        }).execute()
        print(f"[CONFIGURE] ✅ Projet GCloud créé: {project_id}")
        time.sleep(8)

        # === ÉTAPE 2 : Activer Firebase sur le projet ===
        sauvegarder_setup(token, {**session, "status": "configuring", "project_id": project_id})
        firebase_svc = build("firebase", "v1beta1", credentials=creds)
        firebase_svc.projects().addFirebase(
            project=f"projects/{project_id}", body={}
        ).execute()
        print(f"[CONFIGURE] ✅ Firebase activé: {project_id}")
        # FIX: délai augmenté — Firebase Auth n'est pas disponible immédiatement
        # L'initialisation côté Google prend 30 à 60 secondes sur un nouveau projet
        time.sleep(30)

        # === ÉTAPE 3 : Créer l'app Android ===
        sauvegarder_setup(token, {**session, "status": "creating_app", "project_id": project_id})
        app_id = ""
        try:
            firebase_svc.projects().androidApps().create(
                parent=f"projects/{project_id}",
                body={"packageName": "com.managerpresence", "displayName": club_name}
            ).execute()
            # Retry loop : l'app peut mettre jusqu'à 30s à apparaître dans la liste
            for attempt in range(8):
                time.sleep(5)
                apps = firebase_svc.projects().androidApps().list(
                    parent=f"projects/{project_id}"
                ).execute()
                app_id = apps["apps"][0]["appId"] if apps.get("apps") else ""
                if app_id:
                    print(f"[CONFIGURE] ✅ App Android créée: {app_id} (tentative {attempt + 1}/8)")
                    break
                print(f"[CONFIGURE] ⏳ App Android pas encore prête, tentative {attempt + 1}/8...")
            if not app_id:
                print(f"[CONFIGURE] ⚠️ App Android: app_id toujours vide après 8 tentatives")
        except Exception as e:
            print(f"[CONFIGURE] ⚠️ App Android: {e}")

        # === ÉTAPE 4 : Activer Firestore en europe-west9 ===
        sauvegarder_setup(token, {**session, "status": "firestore",
            "project_id": project_id, "app_id": app_id})
        try:
            fs_svc = build("firestore", "v1", credentials=creds)
            fs_svc.projects().databases().create(
                parent=f"projects/{project_id}",
                body={"type": "FIRESTORE_NATIVE", "locationId": "europe-west9"},
                databaseId="(default)"
            ).execute()
            print(f"[CONFIGURE] ✅ Firestore activé: {project_id}")
            time.sleep(5)
        except Exception as e:
            print(f"[CONFIGURE] ⚠️ Firestore: {e}")

        # === ÉTAPE 4b : Configurer les règles de sécurité Firestore ===
        try:
            firestore_rules = """rules_version = '2';
service cloud.firestore {
  match /databases/{database}/documents {
    match /{document=**} {
      allow read, write: if request.auth != null;
    }
  }
}"""
            rules_svc = build("firebaserules", "v1", credentials=creds)
            ruleset = rules_svc.projects().rulesets().create(
                name=f"projects/{project_id}",
                body={
                    "source": {
                        "files": [{
                            "name": "firestore.rules",
                            "content": firestore_rules
                        }]
                    }
                }
            ).execute()
            ruleset_name = ruleset.get("name", "")
            if ruleset_name:
                rules_svc.projects().releases().create(
                    name=f"projects/{project_id}",
                    body={
                        "name": f"projects/{project_id}/releases/cloud.firestore",
                        "rulesetName": ruleset_name
                    }
                ).execute()
                print(f"[CONFIGURE] ✅ Règles Firestore configurées")
        except Exception as e:
            print(f"[CONFIGURE] ⚠️ Règles Firestore: {e}")

        # === ÉTAPE 4c : Activer l'authentification (anonyme + Email/Password) ===
        # FIX: ajout de l'auth Email/Password — nécessaire pour les admins ManagerPresence
        try:
            auth_url = f"https://identitytoolkit.googleapis.com/admin/v2/projects/{project_id}/config"
            headers_auth = {
                "Authorization": f"Bearer {creds.token}",
                "Content-Type": "application/json"
            }
            auth_body = {
                "signIn": {
                    "anonymous": {"enabled": True},
                    "email": {"enabled": True, "passwordRequired": True}
                }
            }
            auth_resp = http_requests.patch(
                auth_url,
                headers=headers_auth,
                json=auth_body,
                params={"updateMask": "signIn.anonymous.enabled,signIn.email.enabled,signIn.email.passwordRequired"}
            )
            if auth_resp.status_code == 200:
                print(f"[CONFIGURE] ✅ Auth anonyme + Email/Password activée")
            else:
                print(f"[CONFIGURE] ⚠️ Auth: {auth_resp.status_code} {auth_resp.text[:200]}")
        except Exception as e:
            print(f"[CONFIGURE] ⚠️ Auth: {e}")

        # === ÉTAPE 5 : Récupérer l'API key ===
        sauvegarder_setup(token, {**session, "status": "api_key",
            "project_id": project_id, "app_id": app_id})
        api_key = ""
        try:
            keys_svc = build("apikeys", "v2", credentials=creds)
            keys_resp = keys_svc.projects().locations().keys().list(
                parent=f"projects/{project_id}/locations/global"
            ).execute()
            if keys_resp.get("keys"):
                key_detail = keys_svc.projects().locations().keys().getKeyString(
                    name=keys_resp["keys"][0]["name"]
                ).execute()
                api_key = key_detail.get("keyString", "")
                print(f"[CONFIGURE] ✅ API key récupérée")
        except Exception as e:
            print(f"[CONFIGURE] ⚠️ API key: {e}")

        # === ÉTAPE 6 : Créer licence trial + marquer comme complete ===
        # FIX: on ne génère plus de mot de passe ici, ni d'email de confirmation.
        # Le mot de passe SU est choisi par l'utilisateur dans setup_finalize(),
        # qui envoie lui-même l'email de confirmation avec le vrai mot de passe.
        licence = creer_licence_trial(project_id, club_name)
        sauvegarder_licence(project_id, licence)

        sauvegarder_setup(token, {
            **session,
            "status":              "complete",
            "project_id":          project_id,
            "app_id":              app_id,
            "api_key":             api_key,
            "is_first_connection": True,
            # su_password_hash sera défini par setup_finalize()
        })
        print(f"[CONFIGURE] 🎉 Terminé ! project_id={project_id}, app_id={app_id}")

        # Notifier l'admin (sans mot de passe — il n'est pas encore défini)
        try:
            envoyer_notification(
                "✅ Structure créée avec succès",
                f"Structure: {club_name}\nGmail: {gmail}\nProject: {project_id}\nApp: {app_id}\n\n(Le mot de passe SU sera défini par l'utilisateur à l'étape suivante)"
            )
        except Exception as e:
            print(f"[CONFIGURE] ⚠️ Notification admin: {e}")

    except Exception as e:
        import traceback
        print(f"[CONFIGURE] ❌ Erreur: {traceback.format_exc()}")
        sauvegarder_setup(token, {**session, "status": "error", "error": str(e)})


# ============================================================
# ROUTES — CRÉATION DE STRUCTURE (mode simple)
# ============================================================

@app.route("/create-structure", methods=["POST"])
def create_structure():
    data      = request.get_json() or {}
    club_name = data.get("club_name", "").strip()
    gmail     = data.get("gmail", "").strip().lower()

    if not club_name:
        return jsonify({"error": "Nom de structure manquant"}), 400
    if not gmail or "@" not in gmail or "." not in gmail:
        return jsonify({"error": "Adresse Gmail invalide"}), 400

    token      = generer_token_setup()
    expires_at = int(time.time()) + SETUP_TOKEN_VALIDITY_SECONDS

    session_data = {
        "club_name":        club_name,
        "gmail":            gmail,
        "token":            token,
        "created_at":       datetime.now().isoformat(),
        "expires_at":       expires_at,
        "status":           "pending",
        "project_id":       None,
        "app_id":           None,
        "api_key":          None,
        "su_password_hash": None,
    }

    if not sauvegarder_setup(token, session_data):
        return jsonify({"error": "Erreur serveur"}), 500

    setup_url = f"{SERVER_BASE_URL}/setup/{token}"

    def envoyer_emails():
        envoyer_email_setup(gmail, club_name, setup_url)
        envoyer_notification(
            "🆕 Nouvelle structure en cours de création",
            f"Structure: {club_name}\nGmail: {gmail}\nToken: {token}\nURL setup: {setup_url}"
        )

    threading.Thread(target=envoyer_emails, daemon=True).start()

    return jsonify({
        "success":   True,
        "token":     token,
        "message":   f"Email envoyé à {gmail}. Vérifiez votre boîte mail.",
        "setup_url": setup_url
    }), 201


@app.route("/setup/<token>", methods=["GET"])
def setup_page(token):
    session = charger_setup(token)

    if not session:
        return """<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Lien invalide</title></head>
<body style="font-family:Arial;text-align:center;padding:60px;color:#333">
<h1>🏔️ ManagerPresence</h1>
<h2 style="color:#C62828">❌ Lien invalide ou expiré</h2>
<p>Recommencez la création depuis l'application.</p>
</body></html>""", 404

    if int(time.time()) > session.get("expires_at", 0):
        return """<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Lien expiré</title></head>
<body style="font-family:Arial;text-align:center;padding:60px;color:#333">
<h1>🏔️ ManagerPresence</h1>
<h2 style="color:#E65100">⏱️ Lien expiré</h2>
<p>Ce lien était valable 24 heures. Recommencez depuis l'application.</p>
</body></html>""", 410

    if session.get("status") == "complete":
        return f"""<!DOCTYPE html><html><head><meta charset="utf-8"></head>
<body style="font-family:Arial;text-align:center;padding:60px;color:#333">
<h1>🏔️ ManagerPresence</h1>
<h2 style="color:#2E7D32">✅ Votre espace existe déjà !</h2>
<p>Ouvrez l'application ManagerPresence pour y accéder.</p>
</body></html>"""

    club_name = session.get("club_name", "")
    gmail     = session.get("gmail", "")

    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Créer votre espace — ManagerPresence</title>
  <style>
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{font-family:Arial,sans-serif;background:#F5F5F5;min-height:100vh;
         display:flex;align-items:center;justify-content:center;padding:20px}}
    .card{{background:white;border-radius:16px;padding:40px 32px;
           max-width:420px;width:100%;box-shadow:0 4px 24px rgba(0,0,0,.08);text-align:center}}
    .club{{background:#E3F2FD;border-radius:8px;padding:12px;margin:16px 0;
           font-size:18px;font-weight:bold;color:#1565C0}}
    .gmail{{background:#E8F5E9;border-radius:6px;padding:6px 12px;
            font-size:13px;color:#2E7D32;margin-bottom:16px;display:inline-block}}
    .steps{{text-align:left;background:#F8F9FA;border-radius:8px;padding:16px;
            margin:16px 0;font-size:14px;color:#333;line-height:2.2}}
    .btn{{display:inline-flex;align-items:center;gap:12px;background:white;
          border:2px solid #DADCE0;border-radius:8px;padding:12px 24px;
          font-size:15px;font-weight:bold;color:#333;text-decoration:none;
          width:100%;justify-content:center;cursor:pointer}}
    .btn:hover{{box-shadow:0 2px 8px rgba(0,0,0,.15)}}
    .rgpd{{font-size:11px;color:#aaa;margin-top:16px;line-height:1.5}}
  </style>
</head>
<body>
  <div class="card">
    <div style="font-size:48px;margin-bottom:8px">🏔️</div>
    <h1 style="color:#1565C0;font-size:22px;margin-bottom:4px">ManagerPresence</h1>
    <p style="color:#888;font-size:13px;margin-bottom:16px">Création de votre espace</p>
    <div class="club">📋 {club_name}</div>
    <div class="gmail">📧 {gmail}</div>
    <div class="steps">
      <div>1️⃣ &nbsp;Connectez-vous avec Google</div>
      <div>2️⃣ &nbsp;Autorisez la création Firebase</div>
      <div>3️⃣ &nbsp;Définissez votre mot de passe SU</div>
    </div>
    <p style="color:#555;font-size:14px;margin-bottom:20px">
      Votre espace sera créé sur <strong>votre propre compte Google</strong>.<br>
      Nous n'avons accès à aucune de vos données.
    </p>
    <div style="background:#E8F5E9;border-radius:8px;padding:14px;margin-bottom:16px;text-align:left;font-size:13px;color:#2E7D32">
      <strong>✅ Ce que nous utilisons :</strong><br>
      • Votre email pour créer votre espace Firebase<br>
      • Les droits pour configurer votre projet Google Cloud<br><br>
      <strong>❌ Ce que nous ne faisons PAS :</strong><br>
      • Nous ne lisons pas vos emails ni vos contacts<br>
      • Nous ne stockons pas votre token Google<br>
      • Nous ne revendons aucune donnée<br><br>
      L'accès OAuth est utilisé <strong>une seule fois</strong> lors de la création,
      puis révocable depuis votre compte Google à tout moment.<br><br>
      <a href="/privacy" target="_blank" style="color:#1565C0">📄 Politique de confidentialité complète</a>
    </div>
    <p style="color:#555;font-size:13px;margin-bottom:12px">
      Sur l'écran suivant, Google vous demandera d'autoriser ces deux accès — cochez les deux :
    </p>
    <div style="background:#FFF9C4;border-radius:8px;padding:12px;margin-bottom:16px;font-size:13px;color:#333;text-align:left">
      🔥 <strong>Afficher et administrer Firebase</strong> — pour créer votre projet<br>
      ☁️ <strong>Voir et configurer Google Cloud</strong> — pour activer Firestore
    </div>
    <a class="btn" href="/setup/{token}/oauth">
      <svg width="20" height="20" viewBox="0 0 24 24">
        <path fill="#4285F4" d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z"/>
        <path fill="#34A853" d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z"/>
        <path fill="#FBBC05" d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z"/>
        <path fill="#EA4335" d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z"/>
      </svg>
      Se connecter avec Google
    </a>
    <p class="rgpd">
      Données hébergées en France (Firebase europe-west9).<br>
      Suppression possible depuis l'application à tout moment.
    </p>
  </div>
</body>
</html>"""


@app.route("/setup/<token>/oauth", methods=["GET"])
def setup_oauth_redirect(token):
    session = charger_setup(token)
    if not session:
        return "Session invalide", 404
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        return "OAuth non configuré sur le serveur", 500

    params = {
        "client_id":     GOOGLE_CLIENT_ID,
        "redirect_uri":  f"{SERVER_BASE_URL}/setup/oauth/callback",
        "response_type": "code",
        "scope":         " ".join(GOOGLE_SCOPES),
        "access_type":   "offline",
        "prompt":        "consent",
        "state":         token,
        "login_hint":    session.get("gmail", "")
    }
    oauth_url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params)
    return redirect(oauth_url)


@app.route("/setup/oauth/callback", methods=["GET"])
def setup_oauth_callback():
    code  = request.args.get("code", "")
    token = request.args.get("state", "")
    error = request.args.get("error", "")

    if error:
        return f"""<html><body style="font-family:Arial;text-align:center;padding:60px">
<h2 style="color:#C62828">❌ Autorisation refusée</h2>
<p>Fermez cette page et recommencez depuis l'application.</p>
</body></html>""", 400

    session = charger_setup(token)
    if not session:
        return "<html><body>Session invalide ou expirée.</body></html>", 404

    sauvegarder_setup(token, {**session, "oauth_code": code, "status": "oauth_done"})
    club_name = session.get("club_name", "")
    gmail = session.get("gmail", "")

    def echanger_oauth_code():
        try:
            token_resp = http_requests.post("https://oauth2.googleapis.com/token", data={
                "code":          code,
                "client_id":     GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "redirect_uri":  f"{SERVER_BASE_URL}/setup/oauth/callback",
                "grant_type":    "authorization_code"
            })
            token_data = token_resp.json()
            if "error" not in token_data:
                sess = charger_setup(token)
                if sess:
                    sauvegarder_setup(token, {**sess, "token_data": token_data, "status": "oauth_done"})
                    print(f"[OAUTH] ✅ Token échangé pour {token[:8]}... → lancement création projet")
                    threading.Thread(
                        target=_configure_firebase_logic,
                        args=(token, {**sess, "token_data": token_data, "status": "oauth_done"}),
                        daemon=True
                    ).start()
            else:
                print(f"[OAUTH] Erreur échange: {token_data.get('error_description')}")
        except Exception as e:
            print(f"[OAUTH] Erreur: {e}")

    threading.Thread(target=echanger_oauth_code, daemon=True).start()

    firebase_url = f"https://console.firebase.google.com/?hl=fr"
    deep_link = f"managerpresence://setup/{token}"

    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Authentification réussie — ManagerPresence</title>
  <style>
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{font-family:Arial,sans-serif;background:#F5F5F5;min-height:100vh;
         display:flex;align-items:center;justify-content:center;padding:20px}}
    .card{{background:white;border-radius:16px;padding:40px 32px;
           max-width:440px;width:100%;box-shadow:0 4px 24px rgba(0,0,0,.08);text-align:center}}
    .btn-app{{display:block;background:#1565C0;color:white;text-decoration:none;
              border-radius:8px;padding:16px 20px;font-size:16px;font-weight:bold;margin:20px 0}}
    .info{{background:#E8F5E9;border-radius:8px;padding:14px;
           font-size:13px;color:#2E7D32;margin:12px 0;text-align:left}}
    .steps{{background:#FFF9C4;border-radius:8px;padding:14px;
            font-size:12px;color:#333;text-align:left;line-height:1.9;margin:12px 0}}
  </style>
</head>
<body>
  <div class="card">
    <div style="font-size:48px;margin-bottom:8px">✅</div>
    <h2 style="color:#2E7D32;margin-bottom:8px">Compte Google connecté !</h2>
    <p style="color:#555;font-size:14px;margin-bottom:16px">
      Bonjour <strong>{gmail}</strong><br>
      Création de votre espace Firebase en cours...
    </p>
    <div class="info">
      📱 <strong>Retournez dans l'application ManagerPresence</strong><br>
      Elle vous guidera pour finaliser la configuration.
    </div>
    <a class="btn-app" href="{deep_link}">
      📱 Retourner dans l'app →
    </a>
    <p style="color:#aaa;font-size:11px;margin:8px 0">
      Si le bouton ne fonctionne pas, revenez manuellement dans l'app.<br>
      Elle reprendra automatiquement.
    </p>
  </div>
<script>
  window.onload = function() {{
    setTimeout(function() {{
      window.location.href = "{deep_link}";
    }}, 1500);
  }};
</script>
</body>
</html>"""


@app.route("/setup/<token>/create", methods=["POST"])
def setup_create_firebase(token):
    session = charger_setup(token)
    if not session:
        return jsonify({"error": "Session invalide"}), 404

    if session.get("status") in ("complete",):
        return jsonify({"success": True, "status": session.get("status")})

    oauth_code = session.get("oauth_code", "")
    if not oauth_code:
        return jsonify({"error": "Code OAuth manquant"}), 400

    try:
        token_resp = http_requests.post("https://oauth2.googleapis.com/token", data={
            "code":          oauth_code,
            "client_id":     GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uri":  f"{SERVER_BASE_URL}/setup/oauth/callback",
            "grant_type":    "authorization_code"
        })
        token_data = token_resp.json()
        if "error" in token_data:
            err = token_data.get("error_description", "OAuth error")
            return jsonify({"error": err}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    sauvegarder_setup(token, {**session, "token_data": token_data, "status": "creating_project"})

    def lancer_configuration():
        with app.app_context():
            try:
                _configure_firebase_logic(token, {**session, "token_data": token_data, "status": "creating_project"})
            except Exception as e:
                import traceback
                print(f"[CONFIGURE BG] Erreur: {traceback.format_exc()}")

    threading.Thread(target=lancer_configuration, daemon=True).start()

    return jsonify({"success": True, "status": "creating_project"})


@app.route("/setup/<token>/configure", methods=["GET"])
def setup_configure_page(token):
    session = charger_setup(token)
    if not session:
        return "Session invalide", 404

    club_name = session.get("club_name", "")

    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Configuration — ManagerPresence</title>
  <style>
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{font-family:Arial;background:#F5F5F5;min-height:100vh;
         display:flex;align-items:center;justify-content:center;padding:20px}}
    .card{{background:white;border-radius:16px;padding:32px 24px;
           max-width:440px;width:100%;box-shadow:0 4px 24px rgba(0,0,0,.08);text-align:center}}
    .spinner{{width:40px;height:40px;border:4px solid #E3F2FD;
              border-top:4px solid #1565C0;border-radius:50%;
              animation:spin 1s linear infinite;margin:16px auto}}
    @keyframes spin{{to{{transform:rotate(360deg)}}}}
    .step{{display:flex;align-items:center;gap:10px;padding:8px 12px;
           border-radius:8px;margin:4px 0;font-size:13px;text-align:left;
           background:#F8F9FA;color:#888}}
    .step.active{{background:#E3F2FD;color:#1565C0;font-weight:bold}}
    .step.done{{background:#E8F5E9;color:#2E7D32}}
    .error-box{{background:#FFEBEE;border-radius:8px;padding:16px;
                margin-top:16px;color:#C62828;font-size:13px;display:none}}
    .retry-btn{{background:#E53935;color:white;border:none;border-radius:8px;
                padding:10px 20px;font-size:14px;cursor:pointer;
                margin-top:12px;display:none}}
    .mountain-wrap{{margin:12px 0 8px 0}}
  </style>
</head>
<body>
  <div class="card">
    <div style="font-size:40px;margin-bottom:8px">🏔️</div>
    <h2 id="title" style="color:#1565C0;margin-bottom:4px">Configuration en cours...</h2>
    <p style="color:#555;font-size:14px;margin-bottom:12px">
      Nous configurons <strong>{club_name}</strong>
    </p>
    <div class="mountain-wrap">
      <svg viewBox="0 0 400 180" xmlns="http://www.w3.org/2000/svg" style="width:100%;height:auto">
        <defs>
          <linearGradient id="sky2" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stop-color="#C5D8F0"/>
            <stop offset="100%" stop-color="#E8F4FD"/>
          </linearGradient>
          <linearGradient id="mtn2" x1="0" y1="0" x2="1" y2="0">
            <stop offset="0%" stop-color="#607D8B"/>
            <stop offset="50%" stop-color="#78909C"/>
            <stop offset="100%" stop-color="#90A4AE"/>
          </linearGradient>
        </defs>
        <rect width="400" height="180" fill="url(#sky2)" rx="12"/>
        <ellipse cx="60" cy="25" rx="30" ry="10" fill="white" opacity="0.7"/>
        <ellipse cx="80" cy="20" rx="20" ry="9" fill="white" opacity="0.7"/>
        <ellipse cx="340" cy="30" rx="25" ry="9" fill="white" opacity="0.6"/>
        <path d="M 0,165 L 60,80 L 85,100 L 120,60 L 160,165 Z" fill="#B0BEC5" opacity="0.8"/>
        <path d="M 60,80 L 48,100 L 72,100 Z" fill="white" opacity="0.9"/>
        <path d="M 240,165 L 290,70 L 320,95 L 350,55 L 400,165 Z" fill="#B0BEC5" opacity="0.8"/>
        <path d="M 350,55 L 338,78 L 362,78 Z" fill="white" opacity="0.9"/>
        <path d="M 60,165 L 110,110 L 140,125 L 170,75 L 195,30 L 210,50 L 230,35 L 255,85 L 275,70 L 310,120 L 340,165 Z" fill="url(#mtn2)"/>
        <path d="M 60,165 L 195,30 L 170,75 L 140,125 L 110,110 Z" fill="#546E7A" opacity="0.5"/>
        <path d="M 195,30 L 175,62 L 200,58 L 215,65 L 230,35 L 212,52 Z" fill="white"/>
        <path d="M 195,30 L 183,50 L 195,48 L 208,52 L 218,38 Z" fill="white" opacity="0.9"/>
        <polygon points="75,165 82,145 89,165" fill="#388E3C"/>
        <polygon points="85,165 92,148 99,165" fill="#2E7D32"/>
        <polygon points="300,165 307,147 314,165" fill="#388E3C"/>
        <polygon points="310,165 317,150 324,165" fill="#2E7D32"/>
        <rect x="0" y="163" width="400" height="17" fill="#5D4037"/>
        <path d="M 75,163 C 110,150 140,135 160,118 C 175,105 183,88 193,65 C 197,50 200,38 203,30"
              fill="none" stroke="white" stroke-width="1.5" stroke-dasharray="5,4" opacity="0.7"/>
        <g id="climber2" transform="translate(75,163)">
          <rect x="-9" y="-16" width="5" height="9" fill="#1565C0" rx="1.5"/>
          <ellipse cx="0" cy="-9" rx="5.5" ry="7" fill="#E53935"/>
          <circle cx="0" cy="-19" r="5" fill="#FFCC80"/>
          <path d="M -5,-19 Q -4,-27 0,-28 Q 4,-27 5,-19" fill="#1565C0"/>
          <rect x="-6" y="-20" width="12" height="3" fill="#1565C0" rx="1"/>
          <line x1="6" y1="-15" x2="13" y2="-24" stroke="#888" stroke-width="1.5"/>
          <line x1="10" y1="-24" x2="16" y2="-21" stroke="#888" stroke-width="2"/>
          <line x1="-5" y1="-12" x2="-10" y2="-8" stroke="#E53935" stroke-width="2"/>
          <line x1="-2" y1="-3" x2="-5" y2="4" stroke="#1565C0" stroke-width="2.5"/>
          <line x1="2" y1="-3" x2="5" y2="4" stroke="#1565C0" stroke-width="2.5"/>
        </g>
        <g id="flag2" opacity="0" transform="translate(203,30)">
          <line x1="0" y1="0" x2="0" y2="-18" stroke="#555" stroke-width="1.5"/>
          <polygon points="0,-18 14,-13 0,-8" fill="#E53935"/>
        </g>
        <text id="pct2" x="200" y="176" text-anchor="middle"
              font-size="11" fill="white" font-weight="bold" font-family="Arial" opacity="0.9">0%</text>
      </svg>
    </div>
    <div id="steps">
      <div class="step active" id="s0">🔍 Recherche de votre projet Firebase</div>
      <div class="step" id="s1">📱 Enregistrement de l'application Android</div>
      <div class="step" id="s2">🔥 Configuration de Firestore</div>
      <div class="step" id="s3">🔒 Règles de sécurité</div>
      <div class="step" id="s4">✅ Finalisation</div>
    </div>
    <div class="spinner" id="spinner" style="margin-top:12px"></div>
    <p style="color:#666;font-size:13px;margin-top:8px" id="msg">Recherche en cours...</p>
    <p style="color:#aaa;font-size:11px;margin-top:8px">
      Cette opération prend environ 60 secondes.<br>Ne fermez pas cette page.
    </p>
    <div class="error-box" id="error-box"></div>
    <button class="retry-btn" id="retry-btn" onclick="window.history.back()">← Retour</button>
  </div>
<script>
var TOKEN = "{token}";
var BASE = "/setup/" + TOKEN;
var polls = 0;
var MAX_POLLS = 80;
var PATH = [
  [75,163],[88,155],[103,145],[118,133],[135,120],
  [150,108],[162,94],[173,78],[183,62],[192,46],[203,30]
];

function setStep(idx) {{
  for (var i = 0; i <= 4; i++) {{
    var el = document.getElementById("s" + i);
    if (!el) continue;
    el.className = "step" + (i < idx ? " done" : i === idx ? " active" : "");
  }}
}}

function setProgress(pct) {{
  var idx = Math.min(Math.floor(pct / 10), PATH.length - 1);
  var x = PATH[idx][0];
  var y = PATH[idx][1];
  document.getElementById("climber2").setAttribute("transform", "translate(" + x + "," + y + ")");
  document.getElementById("pct2").textContent = pct + "%";
  if (pct >= 100) {{
    document.getElementById("flag2").setAttribute("opacity", "1");
  }}
}}

function showError(msg) {{
  document.getElementById("spinner").style.display = "none";
  document.getElementById("title").textContent = "Erreur";
  document.getElementById("title").style.color = "#C62828";
  var eb = document.getElementById("error-box");
  eb.style.display = "block";
  eb.innerHTML = "❌ " + (msg || "Erreur") + "<br><br>Vérifiez votre connexion et réessayez.";
  document.getElementById("retry-btn").style.display = "inline-block";
}}

var STATUS_PROGRESS = {{
  "oauth_done": [0, 5],
  "creating_project": [0, 15],
  "configuring": [1, 35],
  "creating_app": [2, 50],
  "firestore": [2, 65],
  "api_key": [3, 80],
  "complete": [4, 100]
}};

function poll() {{
  polls++;
  if (polls > MAX_POLLS) {{ showError("Délai dépassé. Réessayez."); return; }}
  var xhr = new XMLHttpRequest();
  xhr.open("GET", BASE + "/status", true);
  xhr.onreadystatechange = function() {{
    if (xhr.readyState !== 4) return;
    if (xhr.status === 200) {{
      try {{
        var d = JSON.parse(xhr.responseText);
        var status = d.status || "creating_project";
        var info = STATUS_PROGRESS[status] || [0, 10];
        setStep(info[0]);
        setProgress(info[1]);
        document.getElementById("msg").textContent = d.message || "";
        if (status === "complete") {{
          setProgress(100);
          setTimeout(function() {{ window.location.href = BASE + "/done"; }}, 800);
        }} else if (status === "error") {{
          showError(d.error);
        }} else {{
          setTimeout(poll, 3000);
        }}
      }} catch(e) {{ setTimeout(poll, 3000); }}
    }} else {{ setTimeout(poll, 5000); }}
  }};
  xhr.send();
}}

function start() {{
  var xhr = new XMLHttpRequest();
  xhr.open("POST", BASE + "/configure-firebase", true);
  xhr.onreadystatechange = function() {{
    if (xhr.readyState !== 4) return;
  }};
  xhr.send();
  setTimeout(poll, 2000);
}}

if (document.readyState === "complete" || document.readyState === "interactive") {{
  start();
}} else {{
  document.addEventListener("DOMContentLoaded", start);
}}
</script>
</body>
</html>"""


@app.route("/setup/<token>/status", methods=["GET"])
def setup_status(token):
    session = charger_setup(token)
    if not session:
        return jsonify({"status": "error", "error": "Session invalide"})

    status   = session.get("status", "pending")
    messages = {
        "pending":          "En attente...",
        "oauth_done":       "Authentification Google réussie !",
        "creating_project": "Création de votre projet Google Cloud...",
        "configuring":      "Activation de Firebase sur votre projet...",
        "creating_app":     "Enregistrement de l'application Android...",
        "firestore":        "Activation de Firestore en France (europe-west9)...",
        "api_key":          "Récupération de la clé API...",
        "complete":         "Votre espace est prêt !",
        "error":            session.get("error", "Erreur inconnue")
    }
    return jsonify({
        "status":  status,
        "message": messages.get(status, status),
        "error":   session.get("error") if status == "error" else None
    })


@app.route("/setup/<token>/done", methods=["GET"])
def setup_done_page(token):
    session = charger_setup(token)
    if not session or session.get("status") not in ("complete",):
        return redirect(f"/setup/{token}")

    club_name = session.get("club_name", "")

    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Mot de passe SU — ManagerPresence</title>
  <style>
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{font-family:Arial;background:#F5F5F5;min-height:100vh;
         display:flex;align-items:center;justify-content:center;padding:20px}}
    .card{{background:white;border-radius:16px;padding:40px 32px;
           max-width:420px;width:100%;box-shadow:0 4px 24px rgba(0,0,0,.08)}}
    .warn{{background:#FFF3E0;border-left:4px solid #E65100;border-radius:4px;
           padding:12px;font-size:13px;color:#E65100;margin-bottom:20px;line-height:1.5}}
    input{{width:100%;padding:12px;border:2px solid #E0E0E0;border-radius:8px;
           font-size:16px;margin-bottom:12px;font-family:monospace}}
    input:focus{{outline:none;border-color:#1565C0}}
    button{{width:100%;padding:14px;background:#1565C0;color:white;border:none;
            border-radius:8px;font-size:16px;font-weight:bold;cursor:pointer}}
    button:disabled{{background:#90CAF9;cursor:not-allowed}}
    #msg{{margin-top:12px;font-size:13px;text-align:center}}
    .ok{{color:#2E7D32}}.err{{color:#C62828}}
  </style>
</head>
<body>
  <div class="card">
    <div style="text-align:center;font-size:40px;margin-bottom:16px">🔐</div>
    <h1 style="text-align:center;color:#1565C0;margin-bottom:8px">Mot de passe SU</h1>
    <p style="color:#555;text-align:center;margin-bottom:20px;font-size:14px">
      Structure : <strong>{club_name}</strong>
    </p>
    <div class="warn">
      ⚠️ Ce mot de passe donne accès aux fonctions d'administration avancées.<br>
      <strong>Il ne peut pas être récupéré.</strong> Notez-le précieusement.
    </div>
    <input type="password" id="pwd1" placeholder="Votre mot de passe SU (min. 8 caractères)"
           minlength="8" oninput="verifier()">
    <input type="password" id="pwd2" placeholder="Confirmez le mot de passe"
           minlength="8" oninput="verifier()">
    <button id="btn" onclick="valider()" disabled>✅ Terminer la configuration</button>
    <div id="msg"></div>
    <p style="color:#aaa;font-size:11px;text-align:center;margin-top:16px">
      Minimum 8 caractères.
    </p>
  </div>
  <script>
    function verifier() {{
      const p1 = document.getElementById('pwd1').value;
      const p2 = document.getElementById('pwd2').value;
      const btn = document.getElementById('btn');
      const msg = document.getElementById('msg');
      if (p1.length >= 8 && p1 === p2) {{
        btn.disabled = false; msg.textContent = '';
      }} else if (p2.length > 0 && p1 !== p2) {{
        btn.disabled = true;
        msg.textContent = 'Les mots de passe ne correspondent pas.';
        msg.className = 'err';
      }} else {{ btn.disabled = true; }}
    }}
    async function valider() {{
      const pwd = document.getElementById('pwd1').value;
      const btn = document.getElementById('btn');
      const msg = document.getElementById('msg');
      btn.disabled = true; btn.textContent = 'Enregistrement...';
      const r = await fetch('/setup/{token}/finalize', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{su_password: pwd}})
      }});
      const d = await r.json();
      if (d.success) {{
        msg.textContent = '✅ Vérifiez votre email !'; msg.className = 'ok';
        btn.textContent = '✅ Configuration terminée !';
        setTimeout(() => {{ window.location.href = '/setup/{token}/success'; }}, 2000);
      }} else {{
        msg.textContent = '❌ ' + (d.error || 'Erreur'); msg.className = 'err';
        btn.disabled = false; btn.textContent = '✅ Terminer la configuration';
      }}
    }}
  </script>
</body>
</html>"""


@app.route("/setup/<token>/finalize", methods=["POST"])
def setup_finalize(token):
    """
    Étape finale : Hash du mot de passe SU choisi par l'utilisateur
    + envoi de l'email de confirmation avec le vrai mot de passe.
    C'est le SEUL endroit où l'email de confirmation est envoyé.
    """
    session = charger_setup(token)
    if not session:
        return jsonify({"error": "Session invalide"}), 404

    data        = request.get_json() or {}
    su_password = data.get("su_password", "").strip()

    if len(su_password) < 8:
        return jsonify({"error": "Mot de passe trop court (minimum 8 caractères)"}), 400

    su_hash    = hashlib.sha256(su_password.encode()).hexdigest()
    project_id = session.get("project_id", "")
    club_name  = session.get("club_name", "")
    gmail      = session.get("gmail", "")
    app_id     = session.get("app_id", "")
    api_key    = session.get("api_key", "")

    sauvegarder_setup(token, {
        **session,
        "su_password_hash": su_hash,
        "completed_at":     datetime.now().isoformat()
    })

    # Envoi de l'email de confirmation avec le vrai mot de passe choisi par l'utilisateur
    envoyer_email_confirmation(gmail, club_name, su_password)

    envoyer_notification(
        "✅ Structure finalisée",
        f"Structure: {club_name}\nGmail: {gmail}\nProject: {project_id}"
    )

    return jsonify({
        "success":    True,
        "project_id": project_id,
        "app_id":     app_id,
        "api_key":    api_key,
    })


@app.route("/setup/<token>/success", methods=["GET"])
def setup_success(token):
    session   = charger_setup(token)
    club_name = session.get("club_name", "Votre structure") if session else "Votre structure"
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Espace créé — ManagerPresence</title>
<style>
  body{{font-family:Arial;background:#F5F5F5;min-height:100vh;
       display:flex;align-items:center;justify-content:center;padding:20px}}
  .card{{background:white;border-radius:16px;padding:40px 32px;
         max-width:420px;width:100%;box-shadow:0 4px 24px rgba(0,0,0,.08);text-align:center}}
</style></head>
<body>
  <div class="card">
    <div style="font-size:64px;margin-bottom:16px">✅</div>
    <h1 style="color:#2E7D32;margin-bottom:12px">Votre espace est prêt !</h1>
    <p style="color:#555;font-size:16px;margin-bottom:24px">
      <strong>{club_name}</strong> est opérationnel.
    </p>
    <div style="background:#E8F5E9;border-radius:8px;padding:16px;margin-bottom:20px;
                font-size:14px;color:#2E7D32;line-height:1.8">
      📧 Email de confirmation envoyé avec votre mot de passe SU.<br>
      📱 Ouvrez l'application ManagerPresence.<br>
      🎉 Votre structure apparaît automatiquement.
    </div>
    <p style="color:#aaa;font-size:12px">Vous pouvez fermer cette page.</p>
  </div>
</body></html>"""


@app.route("/credentials/<token>", methods=["GET"])
def get_credentials(token):
    session = charger_setup(token)
    if not session:
        return jsonify({"status": "not_found"}), 404

    status = session.get("status", "pending")
    if status != "complete":
        return jsonify({"status": status, "message": "Création en cours..."})

    return jsonify({
        "status":           "complete",
        "project_id":       session.get("project_id", ""),
        "app_id":           session.get("app_id", ""),
        "api_key":          session.get("api_key", ""),
        "su_password_hash": session.get("su_password_hash", ""),
        "club_name":        session.get("club_name", ""),
    })


@app.route("/setup/<token>/ping", methods=["GET"])
def setup_ping(token):
    session = charger_setup(token)
    if not session:
        return jsonify({"status": "not_found", "ready": False}), 404

    status = session.get("status", "pending")
    ready = status in ("oauth_done", "creating_project", "configuring", "creating_app",
                       "firestore", "api_key", "complete")
    complete = status == "complete"

    resp = {"status": status, "ready": ready, "complete": complete}

    if complete:
        resp.update({
            "project_id":          session.get("project_id", ""),
            "app_id":              session.get("app_id", ""),
            "api_key":             session.get("api_key", ""),
            "su_password_hash":    session.get("su_password_hash", ""),
            "club_name":           session.get("club_name", ""),
            "is_first_connection": session.get("is_first_connection", True),
        })

    return jsonify(resp)


@app.route("/setup/<token>/configure-firebase", methods=["POST"])
def configure_firebase(token):
    session = charger_setup(token)
    if not session:
        return jsonify({"error": "Session invalide"}), 404
    if session.get("status") in ("complete",):
        return jsonify({"success": True})
    token_data = session.get("token_data", {})
    if not token_data:
        return jsonify({"error": "Token OAuth manquant"}), 400

    threading.Thread(
        target=_configure_firebase_logic,
        args=(token, session),
        daemon=True
    ).start()

    return jsonify({"success": True, "status": "configuring"})


@app.route("/resend-setup-email", methods=["POST"])
def resend_setup_email():
    data = request.get_json() or {}
    token = data.get("token", "")
    if not token:
        return jsonify({"error": "Token manquant"}), 400

    session = charger_setup(token)
    if not session:
        return jsonify({"error": "Session invalide ou expirée"}), 404

    gmail = session.get("gmail", "")
    club_name = session.get("club_name", "")
    setup_url = f"{SERVER_BASE_URL}/setup/{token}"

    def envoyer():
        envoyer_email_setup(gmail, club_name, setup_url)

    threading.Thread(target=envoyer, daemon=True).start()
    print(f"[RESEND] Email renvoyé à {gmail}")
    return jsonify({"success": True, "message": f"Email renvoyé à {gmail}"})

# ============================================================
# ROUTES LÉGALES — Politique de confidentialité & CGU
# ============================================================

@app.route("/privacy", methods=["GET"])
def privacy_policy():
    return """<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Politique de confidentialité — ManagerPresence</title>
  <style>
    body { font-family: Arial, sans-serif; max-width: 800px; margin: 0 auto;
           padding: 40px 20px; color: #333; line-height: 1.7; }
    h1 { color: #1565C0; border-bottom: 2px solid #1565C0; padding-bottom: 12px; }
    h2 { color: #1565C0; margin-top: 32px; }
    .date { color: #888; font-size: 14px; margin-bottom: 32px; }
    a { color: #1565C0; }
    .card { background: #F5F5F5; border-radius: 8px; padding: 16px; margin: 16px 0; }
  </style>
</head>
<body>
  <h1>🏔️ ManagerPresence<br>Politique de confidentialité</h1>
  <p class="date">Dernière mise à jour : 12 avril 2026</p>

  <h2>1. Présentation</h2>
  <p>ManagerPresence est une application Android de gestion des présences destinée
  aux structures (clubs, écoles, entreprises). Elle est développée et maintenue par
  Gaëtan Picard (gaetpicard@gmail.com).</p>

  <h2>2. Données collectées</h2>
  <p>Lors de la création d'un espace, nous collectons :</p>
  <ul>
    <li><strong>Adresse email Google (Gmail)</strong> — pour vous identifier et vous envoyer
    les informations de connexion</li>
    <li><strong>Nom de la structure</strong> — pour personnaliser votre espace</li>
  </ul>
  <p>Les données de votre structure (membres, présences, séances) sont hébergées dans
  votre propre projet Firebase, créé sur votre compte Google. Nous n'avons aucun accès
  à ces données.</p>

  <h2>3. Utilisation de Google OAuth</h2>
  <p>ManagerPresence utilise Google OAuth uniquement pour :</p>
  <ul>
    <li>Vous authentifier de manière sécurisée</li>
    <li>Créer un projet Firebase sur votre compte Google Cloud</li>
    <li>Configurer automatiquement votre base de données Firestore</li>
  </ul>
  <div class="card">
    <strong>Important :</strong> Nous ne stockons pas votre token Google.
    L'accès OAuth est utilisé une seule fois lors de la création de votre espace,
    puis les permissions sont révocables depuis votre compte Google à tout moment.
  </div>

  <h2>4. Hébergement des données</h2>
  <ul>
    <li>Votre projet Firebase est hébergé en <strong>France (europe-west9 — Paris)</strong></li>
    <li>Les sessions de création sont temporaires (24h) et supprimées automatiquement</li>
    <li>Aucune donnée personnelle n'est revendue ou partagée avec des tiers</li>
  </ul>

  <h2>5. Vos droits</h2>
  <p>Conformément au RGPD, vous disposez des droits suivants :</p>
  <ul>
    <li><strong>Droit d'accès</strong> — vous pouvez consulter vos données depuis l'application</li>
    <li><strong>Droit de suppression</strong> — vous pouvez supprimer votre espace depuis
    Paramètres → Mon Club → Supprimer ma structure</li>
    <li><strong>Droit de portabilité</strong> — vos données peuvent être exportées
    depuis l'application</li>
  </ul>

  <h2>6. Cookies et traceurs</h2>
  <p>ManagerPresence n'utilise aucun cookie de tracking ou publicitaire.
  Les seules données temporaires stockées sont nécessaires au fonctionnement
  de l'application (session de création, token d'authentification).</p>

  <h2>7. Sécurité</h2>
  <ul>
    <li>Communications chiffrées en HTTPS</li>
    <li>Mots de passe Super Utilisateur hashés en SHA-256</li>
    <li>Règles de sécurité Firestore configurées par structure</li>
  </ul>

  <h2>8. Contact</h2>
  <p>Pour toute question relative à vos données personnelles :</p>
  <div class="card">
    📧 <a href="mailto:gaetpicard@gmail.com">gaetpicard@gmail.com</a><br>
    🏔️ ManagerPresence — Application de gestion des présences
  </div>

  <hr style="margin-top: 40px; border: none; border-top: 1px solid #eee;">
  <p style="color: #aaa; font-size: 12px; text-align: center;">
    ManagerPresence © 2026 — Données hébergées en France (Firebase europe-west9)
  </p>
</body>
</html>"""


@app.route("/cgu", methods=["GET"])
def cgu():
    return """<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>CGU — ManagerPresence</title>
  <style>
    body { font-family: Arial, sans-serif; max-width: 800px; margin: 0 auto;
           padding: 40px 20px; color: #333; line-height: 1.7; }
    h1 { color: #1565C0; border-bottom: 2px solid #1565C0; padding-bottom: 12px; }
    h2 { color: #1565C0; margin-top: 32px; }
    .date { color: #888; font-size: 14px; margin-bottom: 32px; }
  </style>
</head>
<body>
  <h1>🏔️ ManagerPresence<br>Conditions Générales d'Utilisation</h1>
  <p class="date">Dernière mise à jour : 12 avril 2026</p>

  <h2>1. Objet</h2>
  <p>Les présentes CGU régissent l'utilisation de l'application ManagerPresence,
  logiciel de gestion des présences destiné aux structures associatives,
  éducatives et professionnelles.</p>

  <h2>2. Accès au service</h2>
  <p>L'accès à ManagerPresence nécessite un compte Google. En créant un espace,
  vous acceptez que votre adresse Gmail soit utilisée pour la création et la gestion
  de votre espace Firebase.</p>

  <h2>3. Responsabilités</h2>
  <p>En tant qu'administrateur d'une structure, vous êtes responsable de traitement
  au sens du RGPD pour les données de vos membres et employés. ManagerPresence
  agit en qualité de sous-traitant technique.</p>

  <h2>4. Disponibilité</h2>
  <p>ManagerPresence est fourni "en l'état". Nous nous efforçons d'assurer
  une disponibilité maximale mais ne garantissons pas une disponibilité ininterrompue.</p>

  <h2>5. Résiliation</h2>
  <p>Vous pouvez supprimer votre espace à tout moment depuis Paramètres → Mon Club
  → Supprimer ma structure. Cette action est irréversible et supprime toutes vos données.</p>

  <h2>6. Contact</h2>
  <p>📧 <a href="mailto:gaetpicard@gmail.com">gaetpicard@gmail.com</a></p>

  <hr style="margin-top: 40px; border: none; border-top: 1px solid #eee;">
  <p style="color: #aaa; font-size: 12px; text-align: center;">
    ManagerPresence © 2026
  </p>
</body>
</html>"""

# ============================================================
# DÉMARRAGE
# ============================================================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
