"""
ManagerPresence - Serveur de Licences + Stripe
Déployé sur Render.com
Stockage persistant via Firebase Firestore
Paiements via Stripe
Version 2.0.0
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

app = Flask(__name__)
CORS(app)

# ============================================================
# CONFIGURATION
# ============================================================

ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "dev_token_change_me")

SMTP_EMAIL = os.environ.get("SMTP_EMAIL", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
NOTIFY_EMAIL = os.environ.get("NOTIFY_EMAIL", "")

FIREBASE_CREDENTIALS = os.environ.get("FIREBASE_CREDENTIALS", "")

stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_PUBLIC_KEY = os.environ.get("STRIPE_PUBLIC_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

STRIPE_PRICES = {
    "standard_monthly": os.environ.get("STRIPE_PRICE_STANDARD_MONTHLY", ""),
    "standard_yearly":  os.environ.get("STRIPE_PRICE_STANDARD_YEARLY", ""),
    "premium_monthly":  os.environ.get("STRIPE_PRICE_PREMIUM_MONTHLY", ""),
    "premium_yearly":   os.environ.get("STRIPE_PRICE_PREMIUM_YEARLY", ""),
}

PWA_SUCCESS_URL = os.environ.get("PWA_SUCCESS_URL", "https://managerpresence.netlify.app/paiement-reussi")
PWA_CANCEL_URL  = os.environ.get("PWA_CANCEL_URL",  "https://managerpresence.netlify.app/abonnement")

# Initialiser Firebase
if FIREBASE_CREDENTIALS:
    cred_dict = json.loads(FIREBASE_CREDENTIALS)
    cred = credentials.Certificate(cred_dict)
else:
    cred = credentials.Certificate("serviceAccountKey.json")

firebase_admin.initialize_app(cred)
db = firestore.client()

# ============================================================
# DÉFINITION DES PLANS
# ============================================================

PLANS = {
    "trial": {
        "nom": "Essai gratuit (40 jours)",
        "duree_jours": 40,
        "fonctionnalites": [
            "tableau", "eleves", "creneaux", "export", "forum",
            "cadres_illimite", "import", "sms", "perso", "doc",
            "pwa", "stats", "backup_auto", "periodes", "support"
        ],
        "max_cadres": 999,
        "max_membres": 9999,
        "max_creneaux": 9999
    },
    "standard": {
        "nom": "Standard",
        "fonctionnalites": [
            "tableau", "eleves", "creneaux", "forum",
            "email", "backup_manuel", "audit"
        ],
        "max_cadres": 3,
        "max_membres": 25,
        "max_creneaux": 5
    },
    "premium": {
        "nom": "Premium",
        "fonctionnalites": [
            "tableau", "eleves", "creneaux", "export", "forum",
            "cadres_illimite", "import", "sms", "perso", "doc",
            "pwa", "stats", "backup_auto", "periodes", "support",
            "email", "backup_manuel", "audit"
        ],
        "max_cadres": 999,
        "max_membres": 9999,
        "max_creneaux": 9999
    }
}

CODE_TYPES = {
    "PREMIUM_PERMANENT": {"plan": "premium", "jours": 36500, "prefixe": "PRM"},
    "PREMIUM_1AN":       {"plan": "premium", "jours": 365,   "prefixe": "PR1"},
    "STANDARD_1AN":      {"plan": "standard","jours": 365,   "prefixe": "ST1"},
    "PROLONGATION_60J":  {"plan": None,      "jours": 60,    "prefixe": "P60"},
    "PROLONGATION_30J":  {"plan": None,      "jours": 30,    "prefixe": "P30"},
}

PWA_CODE_VALIDITY = 600  # 10 minutes

# ============================================================
# UTILITAIRES - STOCKAGE FIREBASE
# ============================================================

def charger_licences():
    try:
        docs = db.collection("licences").stream()
        return {doc.id: doc.to_dict() for doc in docs}
    except Exception as e:
        print(f"Erreur chargement licences: {e}")
        return {}

def sauvegarder_licence(project_id, licence):
    try:
        db.collection("licences").document(project_id).set(licence)
        return True
    except Exception as e:
        print(f"Erreur sauvegarde licence: {e}")
        return False

def charger_licence(project_id):
    try:
        doc = db.collection("licences").document(project_id).get()
        return doc.to_dict() if doc.exists else None
    except Exception as e:
        print(f"Erreur chargement licence: {e}")
        return None

def charger_codes():
    try:
        docs = db.collection("codes").stream()
        return {doc.id: doc.to_dict() for doc in docs}
    except Exception as e:
        print(f"Erreur chargement codes: {e}")
        return {}

def sauvegarder_code(code, info):
    try:
        db.collection("codes").document(code).set(info)
        return True
    except Exception as e:
        print(f"Erreur sauvegarde code: {e}")
        return False

def charger_code(code):
    try:
        doc = db.collection("codes").document(code).get()
        return doc.to_dict() if doc.exists else None
    except Exception as e:
        print(f"Erreur chargement code: {e}")
        return None

# ============================================================
# UTILITAIRES - CODES PWA
# ============================================================

def sauvegarder_pwa_code(code, data):
    try:
        db.collection("pwa_codes").document(code).set(data)
        return True
    except Exception as e:
        print(f"Erreur sauvegarde code PWA: {e}")
        return False

def charger_pwa_code(code):
    try:
        doc = db.collection("pwa_codes").document(code).get()
        return doc.to_dict() if doc.exists else None
    except Exception as e:
        print(f"Erreur chargement code PWA: {e}")
        return None

def supprimer_pwa_code(code):
    try:
        db.collection("pwa_codes").document(code).delete()
        return True
    except Exception as e:
        print(f"Erreur suppression code PWA: {e}")
        return False

def nettoyer_codes_expires():
    try:
        now = datetime.now().timestamp() * 1000
        expired = db.collection("pwa_codes").where("expiresAt", "<", now).stream()
        for doc in expired:
            doc.reference.delete()
    except Exception as e:
        print(f"Erreur nettoyage codes PWA: {e}")

# ============================================================
# UTILITAIRES - NOTIFICATIONS
# ============================================================

def envoyer_notification(sujet, message):
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
# UTILITAIRES - CODES D'ACTIVATION
# ============================================================

def generer_code(prefixe):
    chars = string.ascii_uppercase + string.digits
    partie1 = ''.join(secrets.choice(chars) for _ in range(4))
    partie2 = ''.join(secrets.choice(chars) for _ in range(4))
    return f"{prefixe}-{partie1}-{partie2}"

# ============================================================
# UTILITAIRES - LICENCES
# ============================================================

def calculer_jours_restants(date_expiration_str):
    try:
        date_exp = datetime.fromisoformat(date_expiration_str.replace("Z", "+00:00"))
        if date_exp.tzinfo:
            date_exp = date_exp.replace(tzinfo=None)
        delta = date_exp - datetime.now()
        return max(0, delta.days)
    except:
        return 0

def creer_licence_trial(project_id, nom_structure=""):
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
    envoyer_notification(
        "🆕 Nouvelle inscription",
        f"Nouveau client inscrit !\n\nProject ID: {project_id}\nStructure: {nom_structure or 'Non renseigné'}\nDate: {maintenant.strftime('%d/%m/%Y %H:%M')}\nExpiration essai: {expiration.strftime('%d/%m/%Y')}"
    )
    return licence

def formater_licence_response(licence):
    jours_restants = calculer_jours_restants(licence.get("dateExpiration", ""))
    est_actif = licence.get("actif", False) and jours_restants > 0

    if not est_actif:
        message = "Votre licence a expiré. Souscrivez un abonnement pour continuer."
    elif jours_restants <= 7:
        message = f"⚠️ Votre licence expire dans {jours_restants} jour(s) !"
    elif jours_restants <= 40 and licence.get("plan") == "trial":
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
    """Route racine pour UptimeRobot"""
    return jsonify({
        "service": "ManagerPresence License Server",
        "status": "ok",
        "version": "2.0.0",
        "timestamp": datetime.now().isoformat()
    })

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "timestamp": datetime.now().isoformat()})

@app.route("/licence/<project_id>", methods=["GET"])
def get_licence(project_id):
    """Récupère la licence (crée un trial si inconnu)"""
    licence = charger_licence(project_id)
    if licence is None:
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

    code_info = charger_code(code)
    if code_info is None:
        return jsonify({"error": "Code invalide"}), 404
    if code_info.get("utilise"):
        return jsonify({"error": "Code déjà utilisé"}), 400

    licence = charger_licence(project_id)
    if licence is None:
        licence = creer_licence_trial(project_id)

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

    code_info["utilise"] = True
    code_info["utilise_par"] = project_id
    code_info["utilise_le"] = datetime.now().isoformat()

    sauvegarder_licence(project_id, licence)
    sauvegarder_code(code, code_info)

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
    """Retourne les prix disponibles pour la PWA"""
    return jsonify({
        "standard": {
            "monthly": {"id": STRIPE_PRICES["standard_monthly"], "price": 4.90, "currency": "eur"},
            "yearly":  {"id": STRIPE_PRICES["standard_yearly"],  "price": 49.90, "currency": "eur"}
        },
        "premium": {
            "monthly": {"id": STRIPE_PRICES["premium_monthly"], "price": 9.99, "currency": "eur"},
            "yearly":  {"id": STRIPE_PRICES["premium_yearly"],  "price": 99.99, "currency": "eur"}
        },
        "publicKey": STRIPE_PUBLIC_KEY
    })

@app.route("/stripe/checkout", methods=["POST"])
def stripe_checkout():
    """
    Crée une session Stripe Checkout.

    Body JSON:
    {
        "projectId": "presence-en-cours",
        "priceId": "price_xxx",
        "email": "client@example.com",
        "nomStructure": "École Vilpy"
    }
    """
    data = request.get_json() or {}
    project_id   = data.get("projectId", "").strip()
    price_id     = data.get("priceId", "").strip()
    email        = data.get("email", "").strip()
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
    """
    Crée une session vers le portail client Stripe.

    Body JSON:
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
    Webhook Stripe.
    URL à configurer dans Stripe Dashboard:
    https://managerpresence-server.onrender.com/stripe/webhook
    """
    payload    = request.get_data(as_text=True)
    sig_header = request.headers.get("Stripe-Signature", "")

    if STRIPE_WEBHOOK_SECRET:
        try:
            event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
        except ValueError:
            return jsonify({"error": "Invalid payload"}), 400
        except stripe.error.SignatureVerificationError:
            return jsonify({"error": "Invalid signature"}), 400
    else:
        event = json.loads(payload)

    event_type  = event.get("type", "")
    data_object = event.get("data", {}).get("object", {})

    print(f"[STRIPE WEBHOOK] Événement: {event_type}")

    if event_type == "checkout.session.completed":
        session         = data_object
        project_id      = session.get("metadata", {}).get("projectId")
        subscription_id = session.get("subscription")
        customer_id     = session.get("customer")
        if project_id and subscription_id:
            handle_subscription_created(project_id, subscription_id, customer_id)

    elif event_type == "customer.subscription.created":
        subscription    = data_object
        project_id      = subscription.get("metadata", {}).get("projectId")
        subscription_id = subscription.get("id")
        customer_id     = subscription.get("customer")
        if project_id:
            handle_subscription_created(project_id, subscription_id, customer_id)

    elif event_type == "customer.subscription.updated":
        subscription = data_object
        project_id   = subscription.get("metadata", {}).get("projectId")
        if project_id:
            handle_subscription_updated(project_id, subscription)

    elif event_type == "customer.subscription.deleted":
        subscription = data_object
        project_id   = subscription.get("metadata", {}).get("projectId")
        if project_id:
            handle_subscription_cancelled(project_id)

    elif event_type == "invoice.payment_succeeded":
        invoice         = data_object
        subscription_id = invoice.get("subscription")
        if subscription_id:
            try:
                subscription = stripe.Subscription.retrieve(subscription_id)
                project_id   = subscription.get("metadata", {}).get("projectId")
                if project_id:
                    handle_payment_succeeded(project_id, subscription)
            except Exception as e:
                print(f"Erreur récupération subscription: {e}")

    elif event_type == "invoice.payment_failed":
        invoice         = data_object
        subscription_id = invoice.get("subscription")
        customer_email  = invoice.get("customer_email", "")
        if subscription_id:
            try:
                subscription = stripe.Subscription.retrieve(subscription_id)
                project_id   = subscription.get("metadata", {}).get("projectId")
                if project_id:
                    handle_payment_failed(project_id, customer_email)
            except Exception as e:
                print(f"Erreur récupération subscription: {e}")

    return jsonify({"received": True})

# ---- Handlers Stripe ----

def handle_subscription_created(project_id, subscription_id, customer_id):
    print(f"[STRIPE] Nouvel abonnement pour {project_id}: {subscription_id}")
    try:
        subscription = stripe.Subscription.retrieve(subscription_id)
        price_id     = subscription["items"]["data"][0]["price"]["id"]

        if price_id in [STRIPE_PRICES["premium_monthly"], STRIPE_PRICES["premium_yearly"]]:
            nouveau_plan = "premium"
        else:
            nouveau_plan = "standard"

        interval = subscription["items"]["data"][0]["price"]["recurring"]["interval"]
        jours = 365 if interval == "year" else 31

        licence = charger_licence(project_id)
        if licence:
            plan_config = PLANS[nouveau_plan]
            licence["plan"]               = nouveau_plan
            licence["fonctionnalites"]    = plan_config["fonctionnalites"]
            licence["maxCadres"]          = plan_config["max_cadres"]
            licence["maxMembres"]         = plan_config.get("max_membres", 9999)
            licence["maxCreneaux"]        = plan_config.get("max_creneaux", 9999)
            licence["dateExpiration"]     = (datetime.now() + timedelta(days=jours)).isoformat()
            licence["actif"]              = True
            licence["stripeCustomerId"]   = customer_id
            licence["stripeSubscriptionId"] = subscription_id
            licence["message"]            = f"Merci ! Votre abonnement {plan_config['nom']} est actif."
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
        status   = subscription.get("status")

        nouveau_plan = "premium" if price_id in [STRIPE_PRICES["premium_monthly"], STRIPE_PRICES["premium_yearly"]] else "standard"

        licence = charger_licence(project_id)
        if licence:
            if status == "active":
                plan_config = PLANS[nouveau_plan]
                licence["plan"]            = nouveau_plan
                licence["fonctionnalites"] = plan_config["fonctionnalites"]
                licence["maxCadres"]       = plan_config["max_cadres"]
                licence["maxMembres"]      = plan_config.get("max_membres", 9999)
                licence["maxCreneaux"]     = plan_config.get("max_creneaux", 9999)
                licence["actif"]           = True
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
        licence["actif"]   = True
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
    """
    Génère et stocke un code PWA temporaire.
    Appelé par l'app Android quand un admin génère un code.

    Body JSON:
    {
        "projectId": "presence-en-cours",
        "code": "PRES-AB12",
        "generatedBy": "Jean",
        "clubName": "École Vilpy",
        "firebaseConfig": { ... }
    }
    """
    data = request.get_json() or {}

    for field in ["projectId", "code", "firebaseConfig"]:
        if not data.get(field):
            return jsonify({"error": f"Champ manquant: {field}"}), 400

    project_id    = data["projectId"]
    code          = data["code"].upper()
    generated_by  = data.get("generatedBy", "Admin")
    club_name     = data.get("clubName", "")
    firebase_config = data["firebaseConfig"]

    licence = charger_licence(project_id)
    if licence:
        if licence.get("plan") == "standard":
            return jsonify({"error": "L'accès PWA nécessite une licence Trial ou Premium"}), 403
        if calculer_jours_restants(licence.get("dateExpiration", "")) <= 0:
            return jsonify({"error": "Licence expirée"}), 403

    now        = datetime.now()
    expires_at = now + timedelta(seconds=PWA_CODE_VALIDITY)
    expires_at_ms = int(expires_at.timestamp() * 1000)

    pwa_data = {
        "projectId":     project_id,
        "code":          code,
        "generatedBy":   generated_by,
        "clubName":      club_name,
        "firebaseConfig":firebase_config,
        "createdAt":     now.isoformat(),
        "expiresAt":     expires_at_ms,
        "used":          False
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

    Body JSON:
    { "code": "PRES-AB12" }
    """
    data = request.get_json() or {}
    code = data.get("code", "").strip().upper()

    if not code:
        return jsonify({"error": "Code manquant"}), 400

    pwa_data = charger_pwa_code(code)
    if pwa_data is None:
        return jsonify({"error": "Code invalide ou expiré"}), 404

    now_ms     = int(datetime.now().timestamp() * 1000)
    expires_at = pwa_data.get("expiresAt", 0)

    if now_ms > expires_at:
        supprimer_pwa_code(code)
        return jsonify({"error": "Code expiré"}), 410

    if pwa_data.get("used", False):
        return jsonify({"error": "Code déjà utilisé"}), 400

    pwa_data["used"]   = True
    pwa_data["usedAt"] = datetime.now().isoformat()
    sauvegarder_pwa_code(code, pwa_data)

    project_id   = pwa_data.get("projectId", "")
    licence      = charger_licence(project_id)
    licence_info = formater_licence_response(licence) if licence else None

    return jsonify({
        "success":       True,
        "projectId":     project_id,
        "clubName":      pwa_data.get("clubName", ""),
        "firebaseConfig":pwa_data.get("firebaseConfig", {}),
        "generatedBy":   pwa_data.get("generatedBy", ""),
        "licence":       licence_info
    })

@app.route("/pwa/status/<code>", methods=["GET"])
def pwa_status(code):
    """
    Vérifie le statut d'un code PWA (pour l'app Android).
    Permet de savoir si le code a été utilisé.
    """
    code     = code.upper()
    pwa_data = charger_pwa_code(code)

    if pwa_data is None:
        return jsonify({"exists": False, "status": "not_found"})

    now_ms     = int(datetime.now().timestamp() * 1000)
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
        "exists":           True,
        "status":           "active",
        "remainingSeconds": remaining_seconds
    })

# ============================================================
# ROUTES ADMIN (protégées par token)
# ============================================================

def verifier_admin():
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

    data      = request.get_json() or {}
    code_type = data.get("type", "").upper()

    if code_type not in CODE_TYPES:
        return jsonify({"error": f"Type invalide. Types: {list(CODE_TYPES.keys())}"}), 400

    config = CODE_TYPES[code_type]
    codes  = charger_codes()

    nouveau_code = generer_code(config["prefixe"])
    while nouveau_code in codes:
        nouveau_code = generer_code(config["prefixe"])

    code_info = {
        "type":    code_type,
        "cree_le": datetime.now().isoformat(),
        "utilise": False
    }
    sauvegarder_code(nouveau_code, code_info)

    return jsonify({
        "code":  nouveau_code,
        "type":  code_type,
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
    """Liste tous les codes PWA (admin)"""
    if not verifier_admin():
        return jsonify({"error": "Non autorisé"}), 401
    try:
        docs   = db.collection("pwa_codes").stream()
        codes  = []
        now_ms = int(datetime.now().timestamp() * 1000)
        for doc in docs:
            data       = doc.to_dict()
            expires_at = data.get("expiresAt", 0)
            status     = "expired" if now_ms > expires_at else ("used" if data.get("used") else "active")
            codes.append({
                "code":        doc.id,
                "projectId":   data.get("projectId", ""),
                "clubName":    data.get("clubName", ""),
                "generatedBy": data.get("generatedBy", ""),
                "createdAt":   data.get("createdAt", ""),
                "status":      status
            })
        codes.sort(key=lambda x: x.get("createdAt", ""), reverse=True)
        return jsonify({"total": len(codes), "codes": codes})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/licence/<project_id>", methods=["POST"])
def admin_update_licence(project_id):
    """Met à jour une licence (admin) - endpoint legacy"""
    if not verifier_admin():
        return jsonify({"error": "Non autorisé"}), 401

    data    = request.get_json() or {}
    licence = charger_licence(project_id)
    if licence is None:
        return jsonify({"error": "Licence non trouvée"}), 404

    if "plan" in data and data["plan"] in PLANS:
        nouveau_plan       = data["plan"]
        plan_config        = PLANS[nouveau_plan]
        licence["plan"]    = nouveau_plan
        licence["fonctionnalites"] = plan_config["fonctionnalites"]
        licence["maxCadres"]       = plan_config["max_cadres"]
        licence["maxMembres"]      = plan_config.get("max_membres", 9999)
        licence["maxCreneaux"]     = plan_config.get("max_creneaux", 9999)

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

    data    = request.get_json() or {}
    licence = charger_licence(project_id)
    if licence is None:
        return jsonify({"error": "Licence non trouvée"}), 404

    if "plan" in data and data["plan"] in PLANS:
        nouveau_plan = data["plan"]
        plan_config  = PLANS[nouveau_plan]
        licence["plan"]            = nouveau_plan
        licence["fonctionnalites"] = plan_config["fonctionnalites"]
        if "maxCadres" not in data:
            licence["maxCadres"]   = plan_config["max_cadres"]
        licence["maxMembres"]      = plan_config.get("max_membres", 9999)
        licence["maxCreneaux"]     = plan_config.get("max_creneaux", 9999)

    if "duree" in data:
        licence["dateExpiration"] = (datetime.now() + timedelta(days=int(data["duree"]))).isoformat()
        licence["actif"] = True
    if "maxCadres" in data:
        licence["maxCadres"] = int(data["maxCadres"])
    if "nomStructure" in data:
        licence["nomStructure"] = data["nomStructure"]

    sauvegarder_licence(project_id, licence)
    envoyer_notification(
        "✏️ Licence modifiée",
        f"Licence modifiée manuellement.\n\nProject ID: {project_id}\nPlan: {licence.get('plan')}\nExpiration: {licence.get('dateExpiration')}"
    )
    return jsonify({"success": True, "licence": formater_licence_response(licence)})

# ============================================================
# DÉMARRAGE
# ============================================================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
