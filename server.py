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
        "version": "2.0.1",
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
    """
    Crée une session Stripe Checkout pour un abonnement.
    
    Body JSON attendu:
    {
        "projectId": "presence-en-cours",
        "priceId": "price_xxx",
        "email": "client@example.com",
        "nomStructure": "École Vilpy"
    }
    """
    data = request.get_json() or {}
    
    project_id = data.get("projectId", "").strip()
    price_id = data.get("priceId", "").strip()
    email = data.get("email", "").strip()
    nom_structure = data.get("nomStructure", "").strip()
    
    if not project_id or not price_id:
        return jsonify({"error": "projectId et priceId requis"}), 400
    
    # Vérifier que le prix est valide
    valid_prices = list(STRIPE_PRICES.values())
    if price_id not in valid_prices:
        return jsonify({"error": "Prix invalide"}), 400
    
    # Charger ou créer la licence
    licence = charger_licence(project_id)
    if licence is None:
        licence = creer_licence_trial(project_id, nom_structure)
        sauvegarder_licence(project_id, licence)
    
    try:
        # Créer ou récupérer le client Stripe
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
        
        # Créer la session Checkout
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
    """
    Crée une session vers le portail client Stripe.
    Permet au client de gérer son abonnement (annuler, changer de carte, etc.)
    
    Body JSON attendu:
    {
        "projectId": "presence-en-cours"
    }
    """
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
    """
    Webhook Stripe pour recevoir les événements de paiement.
    Configure cette URL dans Stripe Dashboard: https://managerpresence-server.onrender.com/stripe/webhook
    """
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
    """Gère la création d'un nouvel abonnement"""
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
    """Gère les modifications d'abonnement"""
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
    """Gère l'annulation d'abonnement"""
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
    """Gère le renouvellement réussi"""
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
    """Gère un échec de paiement"""
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
    """
    Génère et stocke un code PWA temporaire.
    Appelé par l'app Android quand un admin génère un code.
    """
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
    """
    Vérifie un code PWA et retourne la config Firebase si valide.
    Appelé par la PWA quand un utilisateur entre un code.
    """
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
    """
    Vérifie le statut d'un code PWA (pour l'app Android).
    Permet de savoir si le code a été utilisé.
    """
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
    """Liste toutes les licences"""
    if not verifier_admin():
        return jsonify({"error": "Non autorisé"}), 401
    
    licences = charger_licences()
    liste = [formater_licence_response(l) for l in licences.values()]
    liste.sort(key=lambda x: x.get("dateExpiration", ""), reverse=True)
    
    return jsonify({"total": len(liste), "licences": liste})

@app.route("/admin/gencode", methods=["POST"])
def admin_gencode():
    """Génère un nouveau code d'activation"""
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
    """Liste tous les codes"""
    if not verifier_admin():
        return jsonify({"error": "Non autorisé"}), 401
    
    codes = charger_codes()
    liste = [{"code": c, **info} for c, info in codes.items()]
    liste.sort(key=lambda x: x.get("cree_le", ""), reverse=True)
    
    return jsonify({"total": len(liste), "codes": liste})

@app.route("/admin/pwa-codes", methods=["GET"])
def admin_pwa_codes():
    """Liste tous les codes PWA actifs (admin)"""
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
    """Met à jour une licence (admin)"""
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
    """Modifie une licence existante (admin) - endpoint dédié"""
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
    
    # Log console au lieu d'email (évite le timeout SMTP)
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

def envoyer_email_setup(gmail, club_name, setup_url, lang="FR"):
    """Envoie l'email de setup via Brevo API dans la langue de l'utilisateur"""
    if not BREVO_API_KEY:
        print(f"[SETUP EMAIL] BREVO_API_KEY manquant — URL: {setup_url}")
        return True

    i18n = {
        "FR": {
            "subject": f"Créez votre espace {club_name} — ManagerPresence",
            "title": "Votre espace est presque prêt !",
            "body": f"La structure <strong>\"{club_name}\"</strong> a été initialisée.<br>Il ne reste qu'une étape : vous connecter avec votre compte Google.",
            "btn": "Finaliser la création →",
            "validity": "Ce lien est valable 24 heures.<br>Si vous n'êtes pas à l'origine de cette demande, ignorez cet email.",
            "footer": "ManagerPresence — Données hébergées en France (Firebase europe-west9)"
        },
        "EN": {
            "subject": f"Create your {club_name} space — ManagerPresence",
            "title": "Your space is almost ready!",
            "body": f"The structure <strong>\"{club_name}\"</strong> has been initialized.<br>One step left: sign in with your Google account.",
            "btn": "Complete setup →",
            "validity": "This link is valid for 24 hours.<br>If you did not make this request, please ignore this email.",
            "footer": "ManagerPresence — Data hosted in France (Firebase europe-west9)"
        },
        "ES": {
            "subject": f"Cree su espacio {club_name} — ManagerPresence",
            "title": "¡Su espacio está casi listo!",
            "body": f"La estructura <strong>\"{club_name}\"</strong> ha sido inicializada.<br>Solo queda un paso: iniciar sesión con su cuenta de Google.",
            "btn": "Finalizar la creación →",
            "validity": "Este enlace es válido durante 24 horas.<br>Si no realizó esta solicitud, ignore este correo.",
            "footer": "ManagerPresence — Datos alojados en Francia (Firebase europe-west9)"
        },
        "DE": {
            "subject": f"Erstellen Sie Ihren {club_name} Bereich — ManagerPresence",
            "title": "Ihr Bereich ist fast fertig!",
            "body": f"Die Einrichtung <strong>\"{club_name}\"</strong> wurde initialisiert.<br>Noch ein Schritt: Melden Sie sich mit Ihrem Google-Konto an.",
            "btn": "Einrichtung abschließen →",
            "validity": "Dieser Link ist 24 Stunden gültig.<br>Falls Sie diese Anfrage nicht gestellt haben, ignorieren Sie diese E-Mail.",
            "footer": "ManagerPresence — Daten in Frankreich gehostet (Firebase europe-west9)"
        },
        "IT": {
            "subject": f"Crea il tuo spazio {club_name} — ManagerPresence",
            "title": "Il tuo spazio è quasi pronto!",
            "body": f"La struttura <strong>\"{club_name}\"</strong> è stata inizializzata.<br>Manca solo un passo: accedi con il tuo account Google.",
            "btn": "Completa la creazione →",
            "validity": "Questo link è valido per 24 ore.<br>Se non hai effettuato questa richiesta, ignora questa email.",
            "footer": "ManagerPresence — Dati ospitati in Francia (Firebase europe-west9)"
        },
        "PT": {
            "subject": f"Crie o seu espaço {club_name} — ManagerPresence",
            "title": "O seu espaço está quase pronto!",
            "body": f"A estrutura <strong>\"{club_name}\"</strong> foi inicializada.<br>Falta apenas um passo: iniciar sessão com a sua conta Google.",
            "btn": "Finalizar a criação →",
            "validity": "Este link é válido por 24 horas.<br>Se não fez este pedido, ignore este email.",
            "footer": "ManagerPresence — Dados alojados em França (Firebase europe-west9)"
        },
    }
    t = i18n.get(lang, i18n["FR"])

    try:
        html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;padding:20px">
  <h1 style="color:#1565C0;text-align:center">🏔️ ManagerPresence</h1>
  <h2>{t['title']}</h2>
  <p style="color:#555;font-size:16px">{t['body']}</p>
  <div style="text-align:center;margin:30px 0">
    <a href="{setup_url}"
       style="background:#1565C0;color:white;padding:16px 32px;
              text-decoration:none;border-radius:8px;font-size:16px;
              font-weight:bold;display:inline-block">
      {t['btn']}
    </a>
  </div>
  <p style="color:#888;font-size:13px;text-align:center">{t['validity']}</p>
  <hr style="border:none;border-top:1px solid #eee;margin:20px 0">
  <p style="color:#aaa;font-size:12px;text-align:center">{t['footer']}</p>
</body></html>"""

        import urllib.request
        payload = json.dumps({
            "sender": {"name": "ManagerPresence", "email": "cp.support.dev@gmail.com"},
            "to": [{"email": gmail}],
            "subject": t["subject"],
            "htmlContent": html
        }).encode("utf-8")
        req = urllib.request.Request(
            "https://api.brevo.com/v3/smtp/email",
            data=payload,
            headers={"api-key": BREVO_API_KEY, "Content-Type": "application/json", "Accept": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode())
            print(f"[SETUP] Email Brevo envoyé à {gmail} (lang={lang}) — id: {result.get('messageId')}")
        return True
    except Exception as e:
        print(f"[SETUP] Erreur envoi email Brevo: {e}")
        return False

def envoyer_email_confirmation(gmail, club_name, su_password, lang="FR"):
    """Envoie l'email de confirmation avec le mot de passe SU via Brevo dans la langue de l'utilisateur"""
    if not BREVO_API_KEY:
        print(f"[CONFIRMATION] BREVO_API_KEY manquant — MDP: {su_password}")
        return True

    i18n = {
        "FR": {
            "subject": f"✅ Votre espace {club_name} est opérationnel !",
            "ready": "✅ Votre espace est opérationnel !",
            "body": f"La structure <strong>\"{club_name}\"</strong> est prête.",
            "su_title": "🔐 Votre mot de passe Super Utilisateur",
            "su_warning": "⚠️ <strong>Conservez ce mot de passe précieusement.</strong><br>Il ne peut pas être récupéré.",
            "how_title": "Comment accéder à votre espace ?",
            "steps": [f"Ouvrez l'application <strong>ManagerPresence</strong>", f"Votre structure <strong>\"{club_name}\"</strong> apparaît automatiquement", "Utilisez le mot de passe SU ci-dessus pour l'administration"],
            "footer": "ManagerPresence — Données hébergées en France (Firebase europe-west9)"
        },
        "EN": {
            "subject": f"✅ Your {club_name} space is operational!",
            "ready": "✅ Your space is operational!",
            "body": f"The structure <strong>\"{club_name}\"</strong> is ready.",
            "su_title": "🔐 Your Super User password",
            "su_warning": "⚠️ <strong>Keep this password safe.</strong><br>It cannot be recovered.",
            "how_title": "How to access your space?",
            "steps": ["Open the <strong>ManagerPresence</strong> app", f"Your structure <strong>\"{club_name}\"</strong> appears automatically", "Use the SU password above for administration"],
            "footer": "ManagerPresence — Data hosted in France (Firebase europe-west9)"
        },
        "ES": {
            "subject": f"✅ ¡Su espacio {club_name} está operativo!",
            "ready": "✅ ¡Su espacio está operativo!",
            "body": f"La estructura <strong>\"{club_name}\"</strong> está lista.",
            "su_title": "🔐 Su contraseña de Super Usuario",
            "su_warning": "⚠️ <strong>Guarde esta contraseña cuidadosamente.</strong><br>No puede ser recuperada.",
            "how_title": "¿Cómo acceder a su espacio?",
            "steps": ["Abra la aplicación <strong>ManagerPresence</strong>", f"Su estructura <strong>\"{club_name}\"</strong> aparece automáticamente", "Use la contraseña SU de arriba para la administración"],
            "footer": "ManagerPresence — Datos alojados en Francia (Firebase europe-west9)"
        },
        "DE": {
            "subject": f"✅ Ihr {club_name} Bereich ist betriebsbereit!",
            "ready": "✅ Ihr Bereich ist betriebsbereit!",
            "body": f"Die Einrichtung <strong>\"{club_name}\"</strong> ist bereit.",
            "su_title": "🔐 Ihr Super-Benutzer-Passwort",
            "su_warning": "⚠️ <strong>Bewahren Sie dieses Passwort sorgfältig auf.</strong><br>Es kann nicht wiederhergestellt werden.",
            "how_title": "Wie greifen Sie auf Ihren Bereich zu?",
            "steps": ["Öffnen Sie die <strong>ManagerPresence</strong>-App", f"Ihre Einrichtung <strong>\"{club_name}\"</strong> erscheint automatisch", "Verwenden Sie das obige SU-Passwort für die Verwaltung"],
            "footer": "ManagerPresence — Daten in Frankreich gehostet (Firebase europe-west9)"
        },
        "IT": {
            "subject": f"✅ Il tuo spazio {club_name} è operativo!",
            "ready": "✅ Il tuo spazio è operativo!",
            "body": f"La struttura <strong>\"{club_name}\"</strong> è pronta.",
            "su_title": "🔐 La tua password Super Utente",
            "su_warning": "⚠️ <strong>Conserva questa password con cura.</strong><br>Non può essere recuperata.",
            "how_title": "Come accedere al tuo spazio?",
            "steps": ["Apri l'app <strong>ManagerPresence</strong>", f"La tua struttura <strong>\"{club_name}\"</strong> appare automaticamente", "Usa la password SU sopra per l'amministrazione"],
            "footer": "ManagerPresence — Dati ospitati in Francia (Firebase europe-west9)"
        },
        "PT": {
            "subject": f"✅ O seu espaço {club_name} está operacional!",
            "ready": "✅ O seu espaço está operacional!",
            "body": f"A estrutura <strong>\"{club_name}\"</strong> está pronta.",
            "su_title": "🔐 A sua palavra-passe Super Utilizador",
            "su_warning": "⚠️ <strong>Guarde esta palavra-passe com cuidado.</strong><br>Não pode ser recuperada.",
            "how_title": "Como aceder ao seu espaço?",
            "steps": ["Abra a aplicação <strong>ManagerPresence</strong>", f"A sua estrutura <strong>\"{club_name}\"</strong> aparece automaticamente", "Use a palavra-passe SU acima para a administração"],
            "footer": "ManagerPresence — Dados alojados em França (Firebase europe-west9)"
        },
    }
    t = i18n.get(lang, i18n["FR"])
    steps_html = "".join(f"<li>{s}</li>" for s in t["steps"])

    try:
        import urllib.request
        html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;padding:20px">
  <h1 style="color:#1565C0;text-align:center">🏔️ ManagerPresence</h1>
  <div style="background:#E8F5E9;border-radius:8px;padding:20px;margin-bottom:20px">
    <h2 style="color:#2E7D32;margin:0">{t['ready']}</h2>
  </div>
  <p style="color:#555;font-size:16px">{t['body']}</p>
  <div style="background:#FFF3E0;border-radius:8px;padding:20px;margin:20px 0;border-left:4px solid #E65100">
    <h3 style="color:#E65100;margin-top:0">{t['su_title']}</h3>
    <div style="background:white;border-radius:4px;padding:12px;text-align:center;
                font-family:monospace;font-size:22px;font-weight:bold;
                color:#E65100;letter-spacing:2px">
      {su_password}
    </div>
    <p style="color:#BF360C;font-size:13px;margin-bottom:0">{t['su_warning']}</p>
  </div>
  <h3>{t['how_title']}</h3>
  <ol style="color:#555;font-size:15px;line-height:1.8">{steps_html}</ol>
  <hr style="border:none;border-top:1px solid #eee;margin:20px 0">
  <p style="color:#aaa;font-size:12px;text-align:center">{t['footer']}</p>
</body></html>"""
        payload = json.dumps({
            "sender": {"name": "ManagerPresence", "email": "cp.support.dev@gmail.com"},
            "to": [{"email": gmail}],
            "subject": t["subject"],
            "htmlContent": html
        }).encode("utf-8")
        req = urllib.request.Request(
            "https://api.brevo.com/v3/smtp/email",
            data=payload,
            headers={"api-key": BREVO_API_KEY, "Content-Type": "application/json", "Accept": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode())
            print(f"[CONFIRMATION] Email Brevo envoyé à {gmail} (lang={lang}) — id: {result.get('messageId')}")
        return True
    except Exception as e:
        print(f"[CONFIRMATION] Erreur envoi email Brevo: {e}")
        return False


def creer_projet_firebase(token_data, club_name, gmail):
    """
    Crée un projet Firebase sur le compte Google de l'utilisateur
    via les APIs Google Cloud avec son access_token OAuth.
    Retourne dict(project_id, app_id, api_key) ou None si erreur.
    """
    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        creds = Credentials(
            token=token_data["access_token"],
            refresh_token=token_data.get("refresh_token"),
            token_uri="https://oauth2.googleapis.com/token",
            client_id=GOOGLE_CLIENT_ID,
            client_secret=GOOGLE_CLIENT_SECRET,
            scopes=GOOGLE_SCOPES
        )

        # 1. Générer un project_id unique
        suffix    = secrets.token_hex(4)
        safe_name = "".join(c.lower() if c.isalnum() else "-" for c in club_name)[:20].strip("-")
        project_id = f"mp-{safe_name}-{suffix}"

        # 2. Créer le projet Google Cloud
        crm = build("cloudresourcemanager", "v1", credentials=creds)
        crm.projects().create(body={
            "projectId": project_id,
            "name":      club_name
        }).execute()
        print(f"[FIREBASE] Projet GCloud créé: {project_id}")
        time.sleep(6)

        # 3. Activer Firebase sur ce projet
        firebase_svc = build("firebase", "v1beta1", credentials=creds)
        firebase_svc.projects().addFirebase(
            project=f"projects/{project_id}", body={}
        ).execute()
        print(f"[FIREBASE] Firebase activé: {project_id}")
        time.sleep(8)

        # 4. Créer l'app Android
        firebase_svc.projects().androidApps().create(
            parent=f"projects/{project_id}",
            body={"packageName": "com.managerpresence", "displayName": club_name}
        ).execute()
        time.sleep(6)

        # Récupérer l'app_id
        apps   = firebase_svc.projects().androidApps().list(
            parent=f"projects/{project_id}"
        ).execute()
        app_id = apps["apps"][0]["appId"] if apps.get("apps") else ""
        print(f"[FIREBASE] App Android: {app_id}")

        # 5. Activer Firestore en europe-west9
        fs_svc = build("firestore", "v1", credentials=creds)
        try:
            fs_svc.projects().databases().create(
                parent=f"projects/{project_id}",
                body={"type": "FIRESTORE_NATIVE", "locationId": "europe-west9"},
                databaseId="(default)"
            ).execute()
            print(f"[FIREBASE] Firestore activé: {project_id}")
            time.sleep(5)
        except Exception as e:
            print(f"[FIREBASE] Firestore (déjà actif ou erreur mineure): {e}")

        # 6. Récupérer l'API key
        api_key = ""
        try:
            keys_svc  = build("apikeys", "v2", credentials=creds)
            keys_resp = keys_svc.projects().locations().keys().list(
                parent=f"projects/{project_id}/locations/global"
            ).execute()
            if keys_resp.get("keys"):
                key_name   = keys_resp["keys"][0]["name"]
                key_detail = keys_svc.projects().locations().keys().getKeyString(
                    name=key_name
                ).execute()
                api_key = key_detail.get("keyString", "")
        except Exception as e:
            print(f"[FIREBASE] Récupération API key: {e}")

        print(f"[FIREBASE] Création terminée — project_id={project_id}")
        return {"project_id": project_id, "app_id": app_id, "api_key": api_key}

    except Exception as e:
        print(f"[FIREBASE] Erreur création: {e}")
        import traceback
        traceback.print_exc()
        return None


# ============================================================
# ROUTES — CRÉATION DE STRUCTURE (mode simple)
# ============================================================

@app.route("/create-structure", methods=["POST"])
def create_structure():
    """
    Étape 1 : L'app Android initie la création.
    Body: { "club_name": str, "gmail": str, "lang": str (optional, default "FR") }
    """
    data      = request.get_json() or {}
    club_name = data.get("club_name", "").strip()
    gmail     = data.get("gmail", "").strip().lower()
    lang      = data.get("lang", "FR").strip().upper()
    if lang not in ("FR", "EN", "ES", "DE", "IT", "PT"):
        lang = "FR"

    if not club_name:
        return jsonify({"error": "Nom de structure manquant"}), 400
    if not gmail or "@" not in gmail or "." not in gmail:
        return jsonify({"error": "Adresse Gmail invalide"}), 400

    token      = generer_token_setup()
    expires_at = int(time.time()) + SETUP_TOKEN_VALIDITY_SECONDS

    session_data = {
        "club_name":        club_name,
        "gmail":            gmail,
        "lang":             lang,
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
        envoyer_email_setup(gmail, club_name, setup_url, lang)
        envoyer_notification(
            "🆕 Nouvelle structure en cours de création",
            f"Structure: {club_name}\nGmail: {gmail}\nLang: {lang}\nToken: {token}\nURL setup: {setup_url}"
        )

    threading.Thread(target=envoyer_emails, daemon=True).start()

    return jsonify({
        "success":   True,
        "token":     token,
        "message":   f"Email envoyé à {gmail}. Vérifiez votre boîte mail.",
        "setup_url": setup_url
    }), 201


@app.route("/setup/<token>", methods=["GET"])
def web_t(lang, key):
    """Traduction pour les pages web générées par le serveur."""
    WEB_I18N = {
        # Page setup principale
        "setup_title":           {"FR": "Créer votre espace — ManagerPresence", "EN": "Create your space — ManagerPresence", "ES": "Crear su espacio — ManagerPresence", "DE": "Ihren Bereich erstellen — ManagerPresence", "IT": "Crea il tuo spazio — ManagerPresence", "PT": "Criar o seu espaço — ManagerPresence"},
        "setup_creating":        {"FR": "Création de votre espace", "EN": "Creating your space", "ES": "Creando su espacio", "DE": "Ihren Bereich erstellen", "IT": "Creazione del tuo spazio", "PT": "A criar o seu espaço"},
        "setup_step1":           {"FR": "1️⃣ Connectez-vous avec Google", "EN": "1️⃣ Sign in with Google", "ES": "1️⃣ Inicie sesión con Google", "DE": "1️⃣ Mit Google anmelden", "IT": "1️⃣ Accedi con Google", "PT": "1️⃣ Inicie sessão com a Google"},
        "setup_step2":           {"FR": "2️⃣ Autorisez la création Firebase", "EN": "2️⃣ Authorize Firebase creation", "ES": "2️⃣ Autorice la creación de Firebase", "DE": "2️⃣ Firebase-Erstellung autorisieren", "IT": "2️⃣ Autorizza la creazione Firebase", "PT": "2️⃣ Autorize a criação do Firebase"},
        "setup_step3":           {"FR": "3️⃣ Définissez votre mot de passe SU", "EN": "3️⃣ Set your SU password", "ES": "3️⃣ Defina su contraseña SU", "DE": "3️⃣ SU-Passwort festlegen", "IT": "3️⃣ Imposta la tua password SU", "PT": "3️⃣ Defina a sua palavra-passe SU"},
        "setup_own_account":     {"FR": "Votre espace sera créé sur <strong>votre propre compte Google</strong>.<br>Nous n'avons accès à aucune de vos données.", "EN": "Your space will be created on <strong>your own Google account</strong>.<br>We have no access to any of your data.", "ES": "Su espacio se creará en <strong>su propia cuenta de Google</strong>.<br>No tenemos acceso a ninguno de sus datos.", "DE": "Ihr Bereich wird auf <strong>Ihrem eigenen Google-Konto</strong> erstellt.<br>Wir haben keinen Zugriff auf Ihre Daten.", "IT": "Il tuo spazio sarà creato sul <strong>tuo account Google</strong>.<br>Non abbiamo accesso ai tuoi dati.", "PT": "O seu espaço será criado na <strong>sua própria conta Google</strong>.<br>Não temos acesso a nenhum dos seus dados."},
        "setup_we_use":          {"FR": "✅ Ce que nous utilisons :", "EN": "✅ What we use:", "ES": "✅ Lo que usamos:", "DE": "✅ Was wir verwenden:", "IT": "✅ Cosa utilizziamo:", "PT": "✅ O que utilizamos:"},
        "setup_use1":            {"FR": "Votre email pour créer votre espace Firebase", "EN": "Your email to create your Firebase space", "ES": "Su email para crear su espacio Firebase", "DE": "Ihre E-Mail zum Erstellen Ihres Firebase-Bereichs", "IT": "La tua email per creare il tuo spazio Firebase", "PT": "O seu email para criar o seu espaço Firebase"},
        "setup_use2":            {"FR": "Les droits pour configurer votre projet Google Cloud", "EN": "Rights to configure your Google Cloud project", "ES": "Los derechos para configurar su proyecto Google Cloud", "DE": "Rechte zum Konfigurieren Ihres Google Cloud-Projekts", "IT": "I diritti per configurare il tuo progetto Google Cloud", "PT": "Os direitos para configurar o seu projeto Google Cloud"},
        "setup_no_use":          {"FR": "❌ Ce que nous ne faisons PAS :", "EN": "❌ What we do NOT do:", "ES": "❌ Lo que NO hacemos:", "DE": "❌ Was wir NICHT tun:", "IT": "❌ Cosa NON facciamo:", "PT": "❌ O que NÃO fazemos:"},
        "setup_no1":             {"FR": "Nous ne lisons pas vos emails ni vos contacts", "EN": "We do not read your emails or contacts", "ES": "No leemos sus emails ni sus contactos", "DE": "Wir lesen weder Ihre E-Mails noch Ihre Kontakte", "IT": "Non leggiamo le tue email né i tuoi contatti", "PT": "Não lemos os seus emails nem os seus contactos"},
        "setup_no2":             {"FR": "Nous ne stockons pas votre token Google", "EN": "We do not store your Google token", "ES": "No almacenamos su token de Google", "DE": "Wir speichern Ihr Google-Token nicht", "IT": "Non memorizziamo il tuo token Google", "PT": "Não armazenamos o seu token Google"},
        "setup_no3":             {"FR": "Nous ne revendons aucune donnée", "EN": "We do not resell any data", "ES": "No revendemos ningún dato", "DE": "Wir verkaufen keine Daten weiter", "IT": "Non rivendiamo nessun dato", "PT": "Não revendemos quaisquer dados"},
        "setup_oauth_once":      {"FR": "L'accès OAuth est utilisé <strong>une seule fois</strong> lors de la création,\nrévocable depuis votre compte Google à tout moment.", "EN": "OAuth access is used <strong>only once</strong> during creation,\nrevocable from your Google account at any time.", "ES": "El acceso OAuth se usa <strong>solo una vez</strong> durante la creación,\nrevocable desde su cuenta de Google en cualquier momento.", "DE": "Der OAuth-Zugriff wird <strong>nur einmal</strong> bei der Erstellung verwendet,\njederzeit über Ihr Google-Konto widerrufbar.", "IT": "L'accesso OAuth viene utilizzato <strong>solo una volta</strong> durante la creazione,\nrevocabile dal tuo account Google in qualsiasi momento.", "PT": "O acesso OAuth é utilizado <strong>apenas uma vez</strong> durante a criação,\nrevogável a partir da sua conta Google a qualquer momento."},
        "setup_privacy":         {"FR": "📄 Politique de confidentialité complète", "EN": "📄 Full privacy policy", "ES": "📄 Política de privacidad completa", "DE": "📄 Vollständige Datenschutzrichtlinie", "IT": "📄 Informativa sulla privacy completa", "PT": "📄 Política de privacidade completa"},
        "setup_checkboxes":      {"FR": "Sur l'écran suivant, Google vous demandera d'autoriser ces deux accès — cochez les deux :", "EN": "On the next screen, Google will ask you to authorize these two accesses — check both:", "ES": "En la siguiente pantalla, Google le pedirá que autorice estos dos accesos — marque ambos:", "DE": "Auf dem nächsten Bildschirm wird Google Sie bitten, diese beiden Zugriffe zu autorisieren — aktivieren Sie beide:", "IT": "Nella schermata successiva, Google ti chiederà di autorizzare questi due accessi — seleziona entrambi:", "PT": "No ecrã seguinte, a Google pedirá que autorize estes dois acessos — selecione ambos:"},
        "setup_check1":          {"FR": "🔥 <strong>Afficher et administrer Firebase</strong> — pour créer votre projet", "EN": "🔥 <strong>View and administer Firebase</strong> — to create your project", "ES": "🔥 <strong>Ver y administrar Firebase</strong> — para crear su proyecto", "DE": "🔥 <strong>Firebase anzeigen und verwalten</strong> — um Ihr Projekt zu erstellen", "IT": "🔥 <strong>Visualizza e amministra Firebase</strong> — per creare il tuo progetto", "PT": "🔥 <strong>Ver e administrar o Firebase</strong> — para criar o seu projeto"},
        "setup_check2":          {"FR": "☁️ <strong>Voir et configurer Google Cloud</strong> — pour activer Firestore", "EN": "☁️ <strong>View and configure Google Cloud</strong> — to activate Firestore", "ES": "☁️ <strong>Ver y configurar Google Cloud</strong> — para activar Firestore", "DE": "☁️ <strong>Google Cloud anzeigen und konfigurieren</strong> — um Firestore zu aktivieren", "IT": "☁️ <strong>Visualizza e configura Google Cloud</strong> — per attivare Firestore", "PT": "☁️ <strong>Ver e configurar o Google Cloud</strong> — para ativar o Firestore"},
        "setup_signin_google":   {"FR": "Se connecter avec Google", "EN": "Sign in with Google", "ES": "Iniciar sesión con Google", "DE": "Mit Google anmelden", "IT": "Accedi con Google", "PT": "Iniciar sessão com a Google"},
        "setup_rgpd":            {"FR": "Données hébergées en France (Firebase europe-west9).<br>Suppression possible depuis l'application à tout moment.", "EN": "Data hosted in France (Firebase europe-west9).<br>Deletion possible from the app at any time.", "ES": "Datos alojados en Francia (Firebase europe-west9).<br>Eliminación posible desde la aplicación en cualquier momento.", "DE": "Daten in Frankreich gehostet (Firebase europe-west9).<br>Löschung jederzeit über die App möglich.", "IT": "Dati ospitati in Francia (Firebase europe-west9).<br>Eliminazione possibile dall'app in qualsiasi momento.", "PT": "Dados alojados em França (Firebase europe-west9).<br>Eliminação possível a partir da aplicação a qualquer momento."},
        # Page callback OAuth
        "oauth_success_title":   {"FR": "Compte Google connecté !", "EN": "Google account connected!", "ES": "¡Cuenta de Google conectada!", "DE": "Google-Konto verbunden!", "IT": "Account Google connesso!", "PT": "Conta Google ligada!"},
        "oauth_creating":        {"FR": "Création de votre espace Firebase en cours...", "EN": "Creating your Firebase space...", "ES": "Creando su espacio Firebase...", "DE": "Ihr Firebase-Bereich wird erstellt...", "IT": "Creazione del tuo spazio Firebase in corso...", "PT": "A criar o seu espaço Firebase..."},
        "oauth_return_app":      {"FR": "📱 Retournez dans l'application ManagerPresence", "EN": "📱 Return to the ManagerPresence app", "ES": "📱 Regrese a la aplicación ManagerPresence", "DE": "📱 Kehren Sie zur ManagerPresence-App zurück", "IT": "📱 Torna all'app ManagerPresence", "PT": "📱 Regresse à aplicação ManagerPresence"},
        "oauth_return_btn":      {"FR": "📱 Retourner dans l'app →", "EN": "📱 Return to app →", "ES": "📱 Regresar a la app →", "DE": "📱 Zur App zurückkehren →", "IT": "📱 Torna all'app →", "PT": "📱 Regressar à app →"},
        "oauth_if_btn_fails":    {"FR": "Si le bouton ne fonctionne pas, revenez manuellement dans l'app.<br>Elle reprendra automatiquement.", "EN": "If the button doesn't work, return manually to the app.<br>It will resume automatically.", "ES": "Si el botón no funciona, regrese manualmente a la app.<br>Se reanudará automáticamente.", "DE": "Falls der Button nicht funktioniert, kehren Sie manuell zur App zurück.<br>Sie wird automatisch fortgesetzt.", "IT": "Se il pulsante non funziona, torna manualmente all'app.<br>Riprenderà automaticamente.", "PT": "Se o botão não funcionar, regresse manualmente à app.<br>Ela retomará automaticamente."},
        # Page progression
        "config_title":          {"FR": "Configuration en cours...", "EN": "Configuration in progress...", "ES": "Configuración en curso...", "DE": "Konfiguration läuft...", "IT": "Configurazione in corso...", "PT": "Configuração em curso..."},
        "config_configuring":    {"FR": "Nous configurons", "EN": "We are configuring", "ES": "Estamos configurando", "DE": "Wir konfigurieren", "IT": "Stiamo configurando", "PT": "Estamos a configurar"},
        "config_duration":       {"FR": "Cette opération prend environ 60 secondes.<br>Ne fermez pas cette page.", "EN": "This operation takes about 60 seconds.<br>Do not close this page.", "ES": "Esta operación tarda unos 60 segundos.<br>No cierre esta página.", "DE": "Dieser Vorgang dauert etwa 60 Sekunden.<br>Schließen Sie diese Seite nicht.", "IT": "Questa operazione richiede circa 60 secondi.<br>Non chiudere questa pagina.", "PT": "Esta operação demora cerca de 60 segundos.<br>Não feche esta página."},
        "config_s0":             {"FR": "🔍 Création du projet Google Cloud", "EN": "🔍 Creating Google Cloud project", "ES": "🔍 Creando proyecto Google Cloud", "DE": "🔍 Google Cloud-Projekt erstellen", "IT": "🔍 Creazione progetto Google Cloud", "PT": "🔍 A criar projeto Google Cloud"},
        "config_s1":             {"FR": "📱 Enregistrement de l'application Android", "EN": "📱 Registering Android app", "ES": "📱 Registrando aplicación Android", "DE": "📱 Android-App registrieren", "IT": "📱 Registrazione app Android", "PT": "📱 A registar a aplicação Android"},
        "config_s2":             {"FR": "🔥 Configuration de Firestore", "EN": "🔥 Configuring Firestore", "ES": "🔥 Configurando Firestore", "DE": "🔥 Firestore konfigurieren", "IT": "🔥 Configurazione di Firestore", "PT": "🔥 A configurar o Firestore"},
        "config_s3":             {"FR": "🔒 Règles de sécurité", "EN": "🔒 Security rules", "ES": "🔒 Reglas de seguridad", "DE": "🔒 Sicherheitsregeln", "IT": "🔒 Regole di sicurezza", "PT": "🔒 Regras de segurança"},
        "config_s4":             {"FR": "✅ Finalisation", "EN": "✅ Finalization", "ES": "✅ Finalización", "DE": "✅ Abschluss", "IT": "✅ Finalizzazione", "PT": "✅ Finalização"},
        "config_timeout":        {"FR": "Délai dépassé. Réessayez.", "EN": "Timeout. Please retry.", "ES": "Tiempo agotado. Inténtelo de nuevo.", "DE": "Zeitüberschreitung. Bitte erneut versuchen.", "IT": "Timeout. Riprova.", "PT": "Tempo esgotado. Tente novamente."},
        "config_retry":          {"FR": "← Retour", "EN": "← Back", "ES": "← Atrás", "DE": "← Zurück", "IT": "← Indietro", "PT": "← Voltar"},
        # Page mot de passe SU
        "su_title":              {"FR": "Mot de passe SU", "EN": "SU Password", "ES": "Contraseña SU", "DE": "SU-Passwort", "IT": "Password SU", "PT": "Palavra-passe SU"},
        "su_structure":          {"FR": "Structure :", "EN": "Structure:", "ES": "Estructura:", "DE": "Einrichtung:", "IT": "Struttura:", "PT": "Estrutura:"},
        "su_warning":            {"FR": "⚠️ Ce mot de passe donne accès aux fonctions d'administration avancées.<br><strong>Il ne peut pas être récupéré.</strong> Notez-le précieusement.", "EN": "⚠️ This password gives access to advanced administration functions.<br><strong>It cannot be recovered.</strong> Keep it safe.", "ES": "⚠️ Esta contraseña da acceso a las funciones de administración avanzadas.<br><strong>No puede recuperarse.</strong> Guárdela con cuidado.", "DE": "⚠️ Dieses Passwort gibt Zugang zu erweiterten Verwaltungsfunktionen.<br><strong>Es kann nicht wiederhergestellt werden.</strong> Bewahren Sie es sorgfältig auf.", "IT": "⚠️ Questa password dà accesso alle funzioni di amministrazione avanzate.<br><strong>Non può essere recuperata.</strong> Conservala con cura.", "PT": "⚠️ Esta palavra-passe dá acesso às funções de administração avançadas.<br><strong>Não pode ser recuperada.</strong> Guarde-a com cuidado."},
        "su_placeholder1":       {"FR": "Votre mot de passe SU (min. 8 caractères)", "EN": "Your SU password (min. 8 characters)", "ES": "Su contraseña SU (mín. 8 caracteres)", "DE": "Ihr SU-Passwort (mind. 8 Zeichen)", "IT": "La tua password SU (min. 8 caratteri)", "PT": "A sua palavra-passe SU (mín. 8 caracteres)"},
        "su_placeholder2":       {"FR": "Confirmez le mot de passe", "EN": "Confirm the password", "ES": "Confirme la contraseña", "DE": "Passwort bestätigen", "IT": "Conferma la password", "PT": "Confirme a palavra-passe"},
        "su_btn":                {"FR": "✅ Terminer la configuration", "EN": "✅ Complete configuration", "ES": "✅ Completar la configuración", "DE": "✅ Konfiguration abschließen", "IT": "✅ Completa la configurazione", "PT": "✅ Concluir a configuração"},
        "su_saving":             {"FR": "Enregistrement...", "EN": "Saving...", "ES": "Guardando...", "DE": "Speichern...", "IT": "Salvataggio...", "PT": "A guardar..."},
        "su_mismatch":           {"FR": "Les mots de passe ne correspondent pas.", "EN": "Passwords do not match.", "ES": "Las contraseñas no coinciden.", "DE": "Passwörter stimmen nicht überein.", "IT": "Le password non corrispondono.", "PT": "As palavras-passe não coincidem."},
        "su_check_email":        {"FR": "✅ Vérifiez votre email !", "EN": "✅ Check your email!", "ES": "✅ ¡Revise su email!", "DE": "✅ Überprüfen Sie Ihre E-Mail!", "IT": "✅ Controlla la tua email!", "PT": "✅ Verifique o seu email!"},
        "su_done":               {"FR": "✅ Configuration terminée !", "EN": "✅ Configuration complete!", "ES": "✅ ¡Configuración completada!", "DE": "✅ Konfiguration abgeschlossen!", "IT": "✅ Configurazione completata!", "PT": "✅ Configuração concluída!"},
        "su_min_chars":          {"FR": "Minimum 8 caractères.", "EN": "Minimum 8 characters.", "ES": "Mínimo 8 caracteres.", "DE": "Mindestens 8 Zeichen.", "IT": "Minimo 8 caratteri.", "PT": "Mínimo 8 caracteres."},
        # Page succès finale
        "success_title":         {"FR": "Votre espace est prêt !", "EN": "Your space is ready!", "ES": "¡Su espacio está listo!", "DE": "Ihr Bereich ist fertig!", "IT": "Il tuo spazio è pronto!", "PT": "O seu espaço está pronto!"},
        "success_body":          {"FR": "est opérationnel.", "EN": "is operational.", "ES": "está operativo.", "DE": "ist betriebsbereit.", "IT": "è operativo.", "PT": "está operacional."},
        "success_email":         {"FR": "📧 Email de confirmation envoyé avec votre mot de passe SU.", "EN": "📧 Confirmation email sent with your SU password.", "ES": "📧 Email de confirmación enviado con su contraseña SU.", "DE": "📧 Bestätigungs-E-Mail mit Ihrem SU-Passwort gesendet.", "IT": "📧 Email di conferma inviata con la tua password SU.", "PT": "📧 Email de confirmação enviado com a sua palavra-passe SU."},
        "success_open_app":      {"FR": "📱 Ouvrez l'application ManagerPresence.", "EN": "📱 Open the ManagerPresence app.", "ES": "📱 Abra la aplicación ManagerPresence.", "DE": "📱 Öffnen Sie die ManagerPresence-App.", "IT": "📱 Apri l'app ManagerPresence.", "PT": "📱 Abra a aplicação ManagerPresence."},
        "success_auto":          {"FR": "🎉 Votre structure apparaît automatiquement.", "EN": "🎉 Your structure appears automatically.", "ES": "🎉 Su estructura aparece automáticamente.", "DE": "🎉 Ihre Einrichtung erscheint automatisch.", "IT": "🎉 La tua struttura appare automaticamente.", "PT": "🎉 A sua estrutura aparece automaticamente."},
        "success_close":         {"FR": "Vous pouvez fermer cette page.", "EN": "You can close this page.", "ES": "Puede cerrar esta página.", "DE": "Sie können diese Seite schließen.", "IT": "Puoi chiudere questa pagina.", "PT": "Pode fechar esta página."},
        # Erreurs communes
        "err_invalid_link":      {"FR": "❌ Lien invalide ou expiré", "EN": "❌ Invalid or expired link", "ES": "❌ Enlace inválido o expirado", "DE": "❌ Ungültiger oder abgelaufener Link", "IT": "❌ Link non valido o scaduto", "PT": "❌ Link inválido ou expirado"},
        "err_restart":           {"FR": "Recommencez la création depuis l'application.", "EN": "Restart the creation from the app.", "ES": "Reinicie la creación desde la aplicación.", "DE": "Starten Sie die Erstellung erneut über die App.", "IT": "Riavvia la creazione dall'app.", "PT": "Recomece a criação a partir da aplicação."},
        "err_expired_link":      {"FR": "⏱️ Lien expiré", "EN": "⏱️ Link expired", "ES": "⏱️ Enlace expirado", "DE": "⏱️ Link abgelaufen", "IT": "⏱️ Link scaduto", "PT": "⏱️ Link expirado"},
        "err_expired_body":      {"FR": "Ce lien était valable 24 heures. Recommencez depuis l'application.", "EN": "This link was valid for 24 hours. Please restart from the app.", "ES": "Este enlace era válido por 24 horas. Reinicie desde la aplicación.", "DE": "Dieser Link war 24 Stunden gültig. Bitte erneut über die App starten.", "IT": "Questo link era valido per 24 ore. Riavvia dall'app.", "PT": "Este link era válido por 24 horas. Recomece a partir da aplicação."},
        "err_already_done":      {"FR": "✅ Votre espace existe déjà !", "EN": "✅ Your space already exists!", "ES": "✅ ¡Su espacio ya existe!", "DE": "✅ Ihr Bereich existiert bereits!", "IT": "✅ Il tuo spazio esiste già!", "PT": "✅ O seu espaço já existe!"},
        "err_already_open_app":  {"FR": "Ouvrez l'application ManagerPresence pour y accéder.", "EN": "Open the ManagerPresence app to access it.", "ES": "Abra la aplicación ManagerPresence para acceder.", "DE": "Öffnen Sie die ManagerPresence-App, um darauf zuzugreifen.", "IT": "Apri l'app ManagerPresence per accedervi.", "PT": "Abra a aplicação ManagerPresence para aceder."},
        "err_denied":            {"FR": "❌ Autorisation refusée", "EN": "❌ Authorization denied", "ES": "❌ Autorización denegada", "DE": "❌ Autorisierung verweigert", "IT": "❌ Autorizzazione negata", "PT": "❌ Autorização recusada"},
        "err_denied_body":       {"FR": "Fermez cette page et recommencez depuis l'application.", "EN": "Close this page and restart from the app.", "ES": "Cierre esta página y reinicie desde la aplicación.", "DE": "Schließen Sie diese Seite und starten Sie erneut über die App.", "IT": "Chiudi questa pagina e riavvia dall'app.", "PT": "Feche esta página e recomece a partir da aplicação."},
    }
    entry = WEB_I18N.get(key, {})
    return entry.get(lang, entry.get("FR", key))


def setup_page(token):
    """Étape 2 : Page HTML affichée quand l'utilisateur clique le lien email."""
    session = charger_setup(token)

    if not session:
        return """<!DOCTYPE html><html><head><meta charset="utf-8">
<title>ManagerPresence</title></head>
<body style="font-family:Arial;text-align:center;padding:60px;color:#333">
<h1>🏔️ ManagerPresence</h1>
<h2 id="title" style="color:#C62828"></h2>
<p id="body"></p>
<script>
var lang = (navigator.language || 'fr').substring(0,2).toLowerCase();
var t = {
  fr: ['❌ Lien invalide ou expiré', 'Recommencez la création depuis l\'application.'],
  en: ['❌ Invalid or expired link', 'Restart the creation from the app.'],
  es: ['❌ Enlace inválido o expirado', 'Reinicie la creación desde la aplicación.'],
  de: ['❌ Ungültiger oder abgelaufener Link', 'Starten Sie die Erstellung erneut über die App.'],
  it: ['❌ Link non valido o scaduto', 'Riavvia la creazione dall\'app.'],
  pt: ['❌ Link inválido ou expirado', 'Recomece a criação a partir da aplicação.']
}[lang] || t.fr;
document.getElementById('title').textContent = t[0];
document.getElementById('body').textContent = t[1];
</script>
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
    lang      = session.get("lang", "FR")
    T = lambda k: web_t(lang, k)

    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{T('setup_title')}</title>
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
    <p style="color:#888;font-size:13px;margin-bottom:16px">{T('setup_creating')}</p>
    <div class="club">📋 {club_name}</div>
    <div class="gmail">📧 {gmail}</div>
    <div class="steps">
      <div>{T('setup_step1')}</div>
      <div>{T('setup_step2')}</div>
      <div>{T('setup_step3')}</div>
    </div>
    <p style="color:#555;font-size:14px;margin-bottom:20px">
      {T('setup_own_account')}
    </p>
    <div style="background:#E8F5E9;border-radius:8px;padding:14px;margin-bottom:16px;text-align:left;font-size:13px;color:#2E7D32">
      <strong>{T('setup_we_use')}</strong><br>
      • {T('setup_use1')}<br>
      • {T('setup_use2')}<br><br>
      <strong>{T('setup_no_use')}</strong><br>
      • {T('setup_no1')}<br>
      • {T('setup_no2')}<br>
      • {T('setup_no3')}<br><br>
      {T('setup_oauth_once')}<br><br>
      <a href="/privacy" target="_blank" style="color:#1565C0">{T('setup_privacy')}</a>
    </div>
    <p style="color:#555;font-size:13px;margin-bottom:12px">
      {T('setup_checkboxes')}
    </p>
    <div style="background:#FFF9C4;border-radius:8px;padding:12px;margin-bottom:16px;font-size:13px;color:#333;text-align:left">
      {T('setup_check1')}<br>
      {T('setup_check2')}
    </div>
    <a class="btn" href="/setup/{token}/oauth">
      <svg width="20" height="20" viewBox="0 0 24 24">
        <path fill="#4285F4" d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z"/>
        <path fill="#34A853" d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z"/>
        <path fill="#FBBC05" d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z"/>
        <path fill="#EA4335" d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z"/>
      </svg>
      {T('setup_signin_google')}
    </a>
    <p class="rgpd">{T('setup_rgpd')}</p>
  </div>
</body>
</html>"""


@app.route("/setup/<token>/oauth", methods=["GET"])
def setup_oauth_redirect(token):
    """Étape 3 : Redirige vers Google OAuth."""
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
    """Étape 4 : Callback OAuth — affiche page pour créer le projet Firebase manuellement."""
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

    # Échanger immédiatement le code OAuth et sauvegarder le token
    # pour que l'app puisse lancer la configuration dès qu'elle revient
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

    deep_link = f"managerpresence://setup/{token}"
    lang = session.get("lang", "FR") if session else "FR"
    T = lambda k: web_t(lang, k)

    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{T('oauth_success_title')} — ManagerPresence</title>
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
  </style>
</head>
<body>
  <div class="card">
    <div style="font-size:48px;margin-bottom:8px">✅</div>
    <h2 style="color:#2E7D32;margin-bottom:8px">{T('oauth_success_title')}</h2>
    <p style="color:#555;font-size:14px;margin-bottom:16px">
      <strong>{gmail}</strong><br>
      {T('oauth_creating')}
    </p>
    <div class="info">
      {T('oauth_return_app')}
    </div>
    <a class="btn-app" href="{deep_link}">{T('oauth_return_btn')}</a>
    <p style="color:#aaa;font-size:11px;margin:8px 0">
      {T('oauth_if_btn_fails')}
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
    """
    Étape 5 : L'utilisateur a créé son projet Firebase manuellement.
    On échange le code OAuth, on liste ses projets et on récupère le plus récent.
    """
    session = charger_setup(token)
    if not session:
        return jsonify({"error": "Session invalide"}), 404

    if session.get("status") in ("complete"):
        return jsonify({"success": True, "status": session.get("status")})

    oauth_code = session.get("oauth_code", "")
    if not oauth_code:
        return jsonify({"error": "Code OAuth manquant"}), 400

    # Échanger le code OAuth
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

    # Sauvegarder le token et lancer la configuration en arrière-plan
    sauvegarder_setup(token, {**session, "token_data": token_data, "status": "creating_project"})

    # Lancer configure-firebase automatiquement en background
    def lancer_configuration():
        with app.app_context():
            try:
                # Appel interne à configure_firebase
                _configure_firebase_logic(token, {**session, "token_data": token_data, "status": "creating_project"})
            except Exception as e:
                import traceback
                print(f"[CONFIGURE BG] Erreur: {traceback.format_exc()}")

    threading.Thread(target=lancer_configuration, daemon=True).start()

    return jsonify({"success": True, "status": "creating_project"})


@app.route("/setup/<token>/configure", methods=["GET"])
def setup_configure_page(token):
    """Page de sélection et configuration du projet Firebase."""
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

    <!-- SVG Montagne -->
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

    <!-- Montagne alpiniste SVG -->
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
      Cette opération prend environ 30 secondes.<br>Ne fermez pas cette page.
    </p>
    <div class="error-box" id="error-box"></div>
    <button class="retry-btn" id="retry-btn" onclick="window.history.back()">← Retour</button>
  </div>
<script>
var TOKEN = "{token}";
var BASE = "/setup/" + TOKEN;
var polls = 0;
var MAX_POLLS = 60;
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
  eb.innerHTML = "❌ " + (msg || "Erreur") + "<br><br>Vérifiez que vous avez bien créé un projet Firebase et réessayez.";
  document.getElementById("retry-btn").style.display = "inline-block";
}}

var STATUS_PROGRESS = {{
  "creating_project": [0, 15],
  "configuring": [1, 30],
  "creating_app": [2, 45],
  "firestore": [2, 55],
  "api_key": [3, 65],
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

// Lancer la configuration automatiquement
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
    """Poll du statut de création."""
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
    """Étape 6 : Page de définition du mot de passe SU."""
    session = charger_setup(token)
    if not session or session.get("status") not in ("complete"):
        return redirect(f"/setup/{token}")

    club_name = session.get("club_name", "")
    lang = session.get("lang", "FR")
    T = lambda k: web_t(lang, k)

    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{T('su_title')} — ManagerPresence</title>
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
    <h1 style="text-align:center;color:#1565C0;margin-bottom:8px">{T('su_title')}</h1>
    <p style="color:#555;text-align:center;margin-bottom:20px;font-size:14px">
      {T('su_structure')} <strong>{club_name}</strong>
    </p>
    <div class="warn">{T('su_warning')}</div>
    <input type="password" id="pwd1" placeholder="{T('su_placeholder1')}"
           minlength="8" oninput="verifier()">
    <input type="password" id="pwd2" placeholder="{T('su_placeholder2')}"
           minlength="8" oninput="verifier()">
    <button id="btn" onclick="valider()" disabled>{T('su_btn')}</button>
    <div id="msg"></div>
    <p style="color:#aaa;font-size:11px;text-align:center;margin-top:16px">
      {T('su_min_chars')}
    </p>
  </div>
  <script>
    var MSG_MISMATCH = "{T('su_mismatch')}";
    var MSG_SAVING = "{T('su_saving')}";
    var MSG_EMAIL = "{T('su_check_email')}";
    var MSG_DONE = "{T('su_done')}";
    function verifier() {{
      const p1 = document.getElementById('pwd1').value;
      const p2 = document.getElementById('pwd2').value;
      const btn = document.getElementById('btn');
      const msg = document.getElementById('msg');
      if (p1.length >= 8 && p1 === p2) {{
        btn.disabled = false; msg.textContent = '';
      }} else if (p2.length > 0 && p1 !== p2) {{
        btn.disabled = true;
        msg.textContent = MSG_MISMATCH;
        msg.className = 'err';
      }} else {{ btn.disabled = true; }}
    }}
    async function valider() {{
      const pwd = document.getElementById('pwd1').value;
      const btn = document.getElementById('btn');
      const msg = document.getElementById('msg');
      btn.disabled = true; btn.textContent = MSG_SAVING;
      const r = await fetch('/setup/{token}/finalize', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{su_password: pwd}})
      }});
      const d = await r.json();
      if (d.success) {{
        msg.textContent = MSG_EMAIL; msg.className = 'ok';
        btn.textContent = MSG_DONE;
        setTimeout(() => {{ window.location.href = '/setup/{token}/success'; }}, 2000);
      }} else {{
        msg.textContent = '❌ ' + (d.error || 'Erreur'); msg.className = 'err';
        btn.disabled = false; btn.textContent = '{T('su_btn')}';
      }}
    }}
  </script>
</body>
</html>"""


@app.route("/setup/<token>/finalize", methods=["POST"])
def setup_finalize(token):
    """Étape 7 : Hash du mot de passe SU + envoi email de confirmation."""
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
    lang       = session.get("lang", "FR")

    sauvegarder_setup(token, {
        **session,
        "status":           "complete",
        "su_password_hash": su_hash,
        "completed_at":     datetime.now().isoformat()
    })

    # Envoyer l'email de confirmation avec le mot de passe SU en clair dans la langue de l'utilisateur
    envoyer_email_confirmation(gmail, club_name, su_password, lang)

    envoyer_notification(
        "✅ Structure créée",
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
    """Étape 8 : Page finale de succès."""
    session   = charger_setup(token)
    club_name = session.get("club_name", "ManagerPresence") if session else "ManagerPresence"
    lang      = session.get("lang", "FR") if session else "FR"
    T = lambda k: web_t(lang, k)
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{T('success_title')} — ManagerPresence</title>
<style>
  body{{font-family:Arial;background:#F5F5F5;min-height:100vh;
       display:flex;align-items:center;justify-content:center;padding:20px}}
  .card{{background:white;border-radius:16px;padding:40px 32px;
         max-width:420px;width:100%;box-shadow:0 4px 24px rgba(0,0,0,.08);text-align:center}}
</style></head>
<body>
  <div class="card">
    <div style="font-size:64px;margin-bottom:16px">✅</div>
    <h1 style="color:#2E7D32;margin-bottom:12px">{T('success_title')}</h1>
    <p style="color:#555;font-size:16px;margin-bottom:24px">
      <strong>{club_name}</strong> {T('success_body')}
    </p>
    <div style="background:#E8F5E9;border-radius:8px;padding:16px;margin-bottom:20px;
                font-size:14px;color:#2E7D32;line-height:1.8">
      {T('success_email')}<br>
      {T('success_open_app')}<br>
      {T('success_auto')}
    </div>
    <p style="color:#aaa;font-size:12px">{T('success_close')}</p>
  </div>
</body></html>"""


@app.route("/credentials/<token>", methods=["GET"])
def get_credentials(token):
    """
    L'app Android poll cet endpoint pour récupérer les credentials
    une fois la création terminée.
    """
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
    """Endpoint léger pour l'app Android — appelé à chaque onResume."""
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


@app.route("/setup/<token>/secure-rules", methods=["POST"])
def setup_secure_rules(token):
    """
    Resserre les règles Firestore après la création du premier admin.
    Passe de 'allow read, write: if true' à 'allow read, write: if request.auth != null'.
    Utilise le refresh_token stocké pour rafraîchir le token OAuth si nécessaire.
    """
    session = charger_setup(token)
    if not session:
        return jsonify({"error": "Session invalide"}), 404

    project_id = session.get("project_id", "")
    if not project_id:
        return jsonify({"error": "project_id manquant"}), 400

    token_data = session.get("token_data", {})
    refresh_token = token_data.get("refresh_token", "")
    if not refresh_token:
        return jsonify({"error": "refresh_token manquant"}), 400

    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        creds = Credentials(
            token=token_data.get("access_token", ""),
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=GOOGLE_CLIENT_ID,
            client_secret=GOOGLE_CLIENT_SECRET,
            scopes=GOOGLE_SCOPES
        )

        # Forcer le rafraîchissement du token (il a peut-être expiré depuis la création)
        try:
            import google.auth.transport.requests
            creds.refresh(google.auth.transport.requests.Request())
            print(f"[SECURE-RULES] 🔑 Token rafraîchi pour {project_id}")
        except Exception as e:
            print(f"[SECURE-RULES] ⚠️ Refresh token: {e}")

        # Règles strictes
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
            body={"source": {"files": [{"name": "firestore.rules", "content": firestore_rules}]}}
        ).execute()
        ruleset_name = ruleset.get("name", "")
        if ruleset_name:
            rules_svc.projects().releases().updateRelease(
                name=f"projects/{project_id}/releases/cloud.firestore",
                body={"release": {
                    "name": f"projects/{project_id}/releases/cloud.firestore",
                    "rulesetName": ruleset_name
                }}
            ).execute()
            print(f"[SECURE-RULES] ✅ Règles strictes appliquées pour {project_id}")
            return jsonify({"success": True, "message": "Règles sécurisées"})
        else:
            return jsonify({"error": "ruleset_name vide"}), 500

    except Exception as e:
        import traceback
        print(f"[SECURE-RULES] ❌ Erreur: {traceback.format_exc()}")
        return jsonify({"error": str(e)}), 500




def _configure_firebase_logic(token, session):
    """
    Crée le projet Firebase de A à Z sur le compte Google de l'utilisateur.
    Pas de retry loop, pas de serviceusage — addFirebase active les APIs tout seul.
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

        # Debug: vérifier que le token est valide
        print(f"[CONFIGURE] 🔑 access_token présent: {bool(token_data.get('access_token'))}")
        print(f"[CONFIGURE] 🔑 refresh_token présent: {bool(token_data.get('refresh_token'))}")
        print(f"[CONFIGURE] 🔑 scopes reçus: {token_data.get('scope', 'AUCUN')}")

        # Forcer le rafraîchissement du token si nécessaire
        if not creds.valid:
            try:
                import google.auth.transport.requests
                creds.refresh(google.auth.transport.requests.Request())
                print(f"[CONFIGURE] 🔑 Token rafraîchi avec succès")
            except Exception as e:
                print(f"[CONFIGURE] ⚠️ Rafraîchissement token: {e}")

        # === ÉTAPE 1 : Créer le projet Google Cloud ===
        sauvegarder_setup(token, {**session, "status": "creating_project"})
        suffix = secrets.token_hex(4)
        safe_name = "".join(c.lower() if c.isalnum() else "-" for c in club_name)[:20].strip("-")
        project_id = f"mp-{safe_name}-{suffix}"
        print(f"[CONFIGURE] 🔨 Création projet GCloud: {project_id}")

        crm = build("cloudresourcemanager", "v1", credentials=creds)
        create_op = crm.projects().create(body={
            "projectId": project_id,
            "name": (club_name + " Club")[:30] if len(club_name) < 4 else club_name[:30]
        }).execute()
        print(f"[CONFIGURE] 📋 Résultat projects.create: {json.dumps(create_op)[:500]}")

        # Poller l'opération de création via operations.get si un nom d'opération est retourné
        op_name = create_op.get("name", "")
        if op_name:
            print(f"[CONFIGURE] ⏳ Polling operation: {op_name}")
            for i in range(30):
                time.sleep(3)
                try:
                    op_result = crm.operations().get(name=op_name).execute()
                    print(f"[CONFIGURE] ⏳ Operation status (tentative {i+1}): done={op_result.get('done')}")
                    if op_result.get("done"):
                        if "error" in op_result:
                            error_info = op_result["error"]
                            raise Exception(f"projects.create échoué: code={error_info.get('code')} message={error_info.get('message')}")
                        print(f"[CONFIGURE] ✅ Projet GCloud créé avec succès: {project_id}")
                        break
                except Exception as e:
                    if "projects.create échoué" in str(e):
                        raise
                    print(f"[CONFIGURE] ⏳ Operation polling erreur: {e}")
            else:
                print(f"[CONFIGURE] ⚠️ Operation pas terminée après 90s")
        else:
            # Pas d'operation name — fallback sur projects.get
            print(f"[CONFIGURE] ⏳ Pas d'operation name, fallback projects.get polling...")
            for i in range(30):
                time.sleep(3)
                try:
                    project_info = crm.projects().get(projectId=project_id).execute()
                    lifecycle = project_info.get("lifecycleState", "")
                    print(f"[CONFIGURE] ⏳ projects.get tentative {i+1}: lifecycle={lifecycle}")
                    if lifecycle == "ACTIVE":
                        print(f"[CONFIGURE] ✅ Projet GCloud actif: {project_id}")
                        break
                except Exception as e:
                    print(f"[CONFIGURE] ⏳ projects.get tentative {i+1} erreur: {e}")
            else:
                print(f"[CONFIGURE] ⚠️ Projet pas accessible après 90s")
        
        # Pause pour la propagation IAM
        time.sleep(5)

        # === ÉTAPE 2 : Activer Firebase sur le projet ===
        sauvegarder_setup(token, {**session, "status": "configuring", "project_id": project_id})
        firebase_svc = build("firebase", "v1beta1", credentials=creds)
        
        # Debug: vérifier les permissions avant addFirebase
        try:
            avail = firebase_svc.availableProjects().list().execute()
            avail_ids = [p.get("projectId", "") for p in avail.get("projectInfo", [])]
            print(f"[CONFIGURE] 📋 Projets disponibles pour addFirebase: {avail_ids}")
            if project_id not in avail_ids:
                print(f"[CONFIGURE] ⚠️ {project_id} n'est PAS dans la liste availableProjects !")
                # Attendre encore un peu
                time.sleep(10)
        except Exception as e:
            print(f"[CONFIGURE] ⚠️ availableProjects check: {e}")
        
        # addFirebase retourne une Operation — on la poll jusqu'à done=true
        print(f"[CONFIGURE] 🔥 Appel addFirebase pour {project_id}...")
        add_op = firebase_svc.projects().addFirebase(
            project=f"projects/{project_id}", body={}
        ).execute()
        print(f"[CONFIGURE] ✅ addFirebase accepté, operation: {add_op.get('name', 'N/A')}")
        
        op_name = add_op.get("name", "")
        if op_name:
            # Poller l'opération addFirebase
            for i in range(30):  # max 60 secondes
                time.sleep(2)
                try:
                    op_status = firebase_svc.operations().get(name=op_name).execute()
                    if op_status.get("done"):
                        if "error" in op_status:
                            raise Exception(f"addFirebase échoué: {op_status['error']}")
                        print(f"[CONFIGURE] ✅ Firebase activé: {project_id} (après {(i+1)*2}s)")
                        break
                except Exception as e:
                    if "done" in str(e) or i > 20:
                        raise
                    pass
            else:
                print(f"[CONFIGURE] ⚠️ addFirebase operation pas terminée après 60s")
        else:
            # Pas d'operation name, on attend un délai fixe
            print(f"[CONFIGURE] ✅ Firebase activé (sync): {project_id}")
            time.sleep(10)

        # === ÉTAPE 3 : Créer l'app Android ===
        sauvegarder_setup(token, {**session, "status": "creating_app", "project_id": project_id})
        app_id = ""
        try:
            firebase_svc.projects().androidApps().create(
                parent=f"projects/{project_id}",
                body={"packageName": "com.managerpresence", "displayName": club_name}
            ).execute()
            time.sleep(6)
            apps = firebase_svc.projects().androidApps().list(
                parent=f"projects/{project_id}"
            ).execute()
            app_id = apps["apps"][0]["appId"] if apps.get("apps") else ""
            print(f"[CONFIGURE] ✅ App Android créée: {app_id}")
        except Exception as e:
            print(f"[CONFIGURE] ⚠️ App Android: {e}")

        # === ÉTAPE 4 : Activer l'API Firestore puis créer la base ===
        sauvegarder_setup(token, {**session, "status": "firestore",
            "project_id": project_id, "app_id": app_id})
        try:
            # Activer l'API Firestore sur le projet utilisateur
            su_svc = build("serviceusage", "v1", credentials=creds)
            su_svc.services().enable(
                name=f"projects/{project_id}/services/firestore.googleapis.com"
            ).execute()
            print(f"[CONFIGURE] ✅ API Firestore activée")
            time.sleep(5)
        except Exception as e:
            print(f"[CONFIGURE] ⚠️ Activation API Firestore: {e}")

        try:
            fs_svc = build("firestore", "v1", credentials=creds)
            fs_svc.projects().databases().create(
                parent=f"projects/{project_id}",
                body={"type": "FIRESTORE_NATIVE", "locationId": "europe-west9"},
                databaseId="(default)"
            ).execute()
            print(f"[CONFIGURE] ✅ Firestore créé: {project_id}")
            time.sleep(5)
        except Exception as e:
            print(f"[CONFIGURE] ⚠️ Firestore: {e}")

        # === ÉTAPE 4b : Règles de sécurité Firestore ===
        try:
            # Activer l'API firebaserules sur le projet utilisateur
            su_svc = build("serviceusage", "v1", credentials=creds)
            su_svc.services().enable(
                name=f"projects/{project_id}/services/firebaserules.googleapis.com"
            ).execute()
            print(f"[CONFIGURE] ✅ API Firebase Rules activée")
            time.sleep(10)  # Laisser l'API se propager (ancien 3s ne suffisait pas)
        except Exception as e:
            print(f"[CONFIGURE] ⚠️ Activation API Firebase Rules: {e}")

        # Règles ouvertes pendant le setup initial
        firestore_rules = """rules_version = '2';
service cloud.firestore {
  match /databases/{database}/documents {
    match /{document=**} {
      allow read, write: if true;
    }
  }
}"""
        # Retry sur la création/déploiement des règles — l'API peut mettre du temps à devenir dispo
        rules_deployed = False
        for rules_attempt in range(6):
            try:
                rules_svc = build("firebaserules", "v1", credentials=creds)
                ruleset = rules_svc.projects().rulesets().create(
                    name=f"projects/{project_id}",
                    body={"source": {"files": [{"name": "firestore.rules", "content": firestore_rules}]}}
                ).execute()
                ruleset_name = ruleset.get("name", "")
                if ruleset_name:
                    rules_svc.projects().releases().create(
                        name=f"projects/{project_id}",
                        body={"name": f"projects/{project_id}/releases/cloud.firestore", "rulesetName": ruleset_name}
                    ).execute()
                    print(f"[CONFIGURE] ✅ Règles Firestore déployées (tentative {rules_attempt+1})")
                    rules_deployed = True
                    break
            except Exception as e:
                print(f"[CONFIGURE] ⏳ Règles Firestore tentative {rules_attempt+1}/6: {e}")
                time.sleep(10)
        
        if not rules_deployed:
            print(f"[CONFIGURE] ❌ ÉCHEC du déploiement des règles Firestore après 6 tentatives !")

        # === ÉTAPE 4c : Activer Identity Toolkit et configurer auth ===
        # Headers communs pour les appels auth — token de l'UTILISATEUR sur son projet
        headers_auth = {"Authorization": f"Bearer {creds.token}", "Content-Type": "application/json"}

        try:
            su_svc = build("serviceusage", "v1", credentials=creds)
            su_svc.services().enable(
                name=f"projects/{project_id}/services/identitytoolkit.googleapis.com"
            ).execute()
            print(f"[CONFIGURE] ✅ API Identity Toolkit activée sur {project_id}")
            time.sleep(8)
        except Exception as e:
            print(f"[CONFIGURE] ⚠️ Activation API Identity Toolkit: {e}")

        # API admin v2 — sur le projet de l'UTILISATEUR (project_id), pas le projet serveur
        try:
            auth_url = f"https://identitytoolkit.googleapis.com/admin/v2/projects/{project_id}/config"
            auth_body = {"signIn": {"anonymous": {"enabled": True}, "email": {"enabled": True, "passwordRequired": True}}}
            auth_resp = http_requests.patch(auth_url, headers=headers_auth, json=auth_body,
                params={"updateMask": "signIn.anonymous.enabled,signIn.email.enabled,signIn.email.passwordRequired"})
            if auth_resp.status_code == 200:
                print(f"[CONFIGURE] ✅ Auth configurée sur {project_id}")
            else:
                print(f"[CONFIGURE] ⚠️ Auth v2: {auth_resp.status_code} {auth_resp.text[:300]}")
        except Exception as e:
            print(f"[CONFIGURE] ⚠️ Auth v2: {e}")

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

        # === ÉTAPE 6 : Licence trial + finaliser ===
        licence = creer_licence_trial(project_id, club_name)
        sauvegarder_licence(project_id, licence)

        # Pas de mot de passe SU côté serveur — l'utilisateur le choisit lui-même
        # lors de "Démarrer le club" dans l'app
        sauvegarder_setup(token, {
            **session,
            "status":              "complete",
            "project_id":          project_id,
            "app_id":              app_id,
            "api_key":             api_key,
            "is_first_connection": True,
        })
        print(f"[CONFIGURE] 🎉 Terminé ! project_id={project_id}, app_id={app_id}")

        try:
            envoyer_notification(
                "✅ Structure créée avec succès",
                f"Structure: {club_name}\nGmail: {gmail}\nProject: {project_id}\nApp: {app_id}"
            )
        except Exception as e:
            print(f"[CONFIGURE] ⚠️ Notification admin: {e}")

    except Exception as e:
        import traceback
        print(f"[CONFIGURE] ❌ Erreur: {traceback.format_exc()}")
        sauvegarder_setup(token, {**session, "status": "error", "error": str(e)})


@app.route("/setup/<token>/configure-firebase", methods=["POST"])
def configure_firebase(token):
    """Configure le projet Firebase — délègue à _configure_firebase_logic."""
    session = charger_setup(token)
    if not session:
        return jsonify({"error": "Session invalide"}), 404
    if session.get("status") in ("complete"):
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
    """Renvoie l'email de setup si l'utilisateur ne l'a pas reçu."""
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
    """Politique de confidentialité — requise pour validation OAuth Google"""
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
    """Conditions Générales d'Utilisation"""
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
