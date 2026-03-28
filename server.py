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

app = Flask(__name__)
CORS(app)

ADMIN_TOKEN       = os.environ.get("ADMIN_TOKEN", "dev_token_change_me")
SMTP_EMAIL        = os.environ.get("SMTP_EMAIL", "")
SMTP_PASSWORD     = os.environ.get("SMTP_PASSWORD", "")
NOTIFY_EMAIL      = os.environ.get("NOTIFY_EMAIL", "")
FIREBASE_CREDENTIALS = os.environ.get("FIREBASE_CREDENTIALS", "")
stripe.api_key    = os.environ.get("STRIPE_SECRET_KEY", "")
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

if FIREBASE_CREDENTIALS:
    cred = credentials.Certificate(json.loads(FIREBASE_CREDENTIALS))
else:
    cred = credentials.Certificate("serviceAccountKey.json")
firebase_admin.initialize_app(cred)
db = firestore.client()

PLANS = {
    "trial": {
        "nom": "Essai gratuit (40 jours)", "duree_jours": 40,
        "fonctionnalites": ["tableau","eleves","creneaux","export","forum","cadres_illimite","import","sms","perso","doc","pwa","stats","backup_auto","periodes","support"],
        "max_cadres": 999, "max_membres": 9999, "max_creneaux": 9999
    },
    "standard": {
        "nom": "Standard",
        "fonctionnalites": ["tableau","eleves","creneaux","forum","email","backup_manuel","audit"],
        "max_cadres": 3, "max_membres": 25, "max_creneaux": 5
    },
    "premium": {
        "nom": "Premium",
        "fonctionnalites": ["tableau","eleves","creneaux","export","forum","cadres_illimite","import","sms","perso","doc","pwa","stats","backup_auto","periodes","support","email","backup_manuel","audit"],
        "max_cadres": 999, "max_membres": 9999, "max_creneaux": 9999
    }
}

CODE_TYPES = {
    "PREMIUM_PERMANENT": {"plan":"premium",  "jours":36500, "prefixe":"PRM"},
    "PREMIUM_1AN":       {"plan":"premium",  "jours":365,   "prefixe":"PR1"},
    "STANDARD_1AN":      {"plan":"standard", "jours":365,   "prefixe":"ST1"},
    "PROLONGATION_60J":  {"plan":None,       "jours":60,    "prefixe":"P60"},
    "PROLONGATION_30J":  {"plan":None,       "jours":30,    "prefixe":"P30"},
}

PWA_CODE_VALIDITY = 600

# ── Firebase helpers ──────────────────────────────────────

def charger_licence(pid):
    try:
        doc = db.collection("licences").document(pid).get()
        return doc.to_dict() if doc.exists else None
    except Exception as e:
        print(f"charger_licence: {e}"); return None

def sauvegarder_licence(pid, data):
    try:
        db.collection("licences").document(pid).set(data); return True
    except Exception as e:
        print(f"sauvegarder_licence: {e}"); return False

def charger_licences():
    try:
        return {d.id: d.to_dict() for d in db.collection("licences").stream()}
    except Exception as e:
        print(f"charger_licences: {e}"); return {}

def charger_code(code):
    try:
        doc = db.collection("codes").document(code).get()
        return doc.to_dict() if doc.exists else None
    except Exception as e:
        print(f"charger_code: {e}"); return None

def sauvegarder_code(code, data):
    try:
        db.collection("codes").document(code).set(data); return True
    except Exception as e:
        print(f"sauvegarder_code: {e}"); return False

def charger_codes():
    try:
        return {d.id: d.to_dict() for d in db.collection("codes").stream()}
    except Exception as e:
        print(f"charger_codes: {e}"); return {}

def charger_pwa_code(code):
    try:
        doc = db.collection("pwa_codes").document(code).get()
        return doc.to_dict() if doc.exists else None
    except Exception as e:
        print(f"charger_pwa_code: {e}"); return None

def sauvegarder_pwa_code(code, data):
    try:
        db.collection("pwa_codes").document(code).set(data); return True
    except Exception as e:
        print(f"sauvegarder_pwa_code: {e}"); return False

def supprimer_pwa_code(code):
    try:
        db.collection("pwa_codes").document(code).delete(); return True
    except Exception as e:
        print(f"supprimer_pwa_code: {e}"); return False

def nettoyer_codes_expires():
    try:
        now = datetime.now().timestamp() * 1000
        for doc in db.collection("pwa_codes").where("expiresAt","<",now).stream():
            doc.reference.delete()
    except Exception as e:
        print(f"nettoyer_codes_expires: {e}")

# ── Notifications ─────────────────────────────────────────

def envoyer_notification(sujet, message):
    if not SMTP_PASSWORD or not SMTP_EMAIL:
        print(f"[NOTIF] {sujet}: {message}"); return False
    try:
        msg = MIMEMultipart()
        msg["From"] = SMTP_EMAIL; msg["To"] = NOTIFY_EMAIL
        msg["Subject"] = f"[ManagerPresence] {sujet}"
        msg.attach(MIMEText(message, "plain"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(SMTP_EMAIL, SMTP_PASSWORD); s.send_message(msg)
        return True
    except Exception as e:
        print(f"envoyer_notification: {e}"); return False

# ── Licences ─────────────────────────────────────────────

def generer_code(prefixe):
    chars = string.ascii_uppercase + string.digits
    return f"{prefixe}-{''.join(secrets.choice(chars) for _ in range(4))}-{''.join(secrets.choice(chars) for _ in range(4))}"

def calculer_jours_restants(date_str):
    try:
        d = datetime.fromisoformat(date_str.replace("Z","+00:00"))
        if d.tzinfo: d = d.replace(tzinfo=None)
        return max(0,(d - datetime.now()).days)
    except: return 0

def creer_licence_trial(pid, nom=""):
    now = datetime.now(); exp = now + timedelta(days=PLANS["trial"]["duree_jours"])
    l = {
        "projectId": pid, "nomStructure": nom,
        "dateInscription": now.isoformat(), "dateExpiration": exp.isoformat(),
        "plan":"trial", "actif":True,
        "fonctionnalites": PLANS["trial"]["fonctionnalites"],
        "maxCadres": PLANS["trial"]["max_cadres"],
        "maxMembres": PLANS["trial"]["max_membres"],
        "maxCreneaux": PLANS["trial"]["max_creneaux"],
        "stripeCustomerId":None, "stripeSubscriptionId":None,
        "message": f"Bienvenue ! Essai gratuit de {PLANS['trial']['duree_jours']} jours."
    }
    envoyer_notification("🆕 Nouvelle inscription",
        f"Project ID: {pid}\nStructure: {nom or 'N/A'}\nDate: {now.strftime('%d/%m/%Y %H:%M')}\nExpiration: {exp.strftime('%d/%m/%Y')}")
    return l

def formater_licence_response(l):
    j = calculer_jours_restants(l.get("dateExpiration",""))
    actif = l.get("actif",False) and j > 0
    if not actif:
        msg = "Licence expirée. Souscrivez un abonnement pour continuer."
    elif j <= 7:
        msg = f"⚠️ Expire dans {j} jour(s) !"
    elif j <= 30 and l.get("plan") == "trial":
        msg = f"Essai gratuit — expire dans {j} jours."
    else:
        msg = l.get("message","")
    p = PLANS.get(l.get("plan","trial"), PLANS["trial"])
    return {
        "projectId": l.get("projectId"), "nomStructure": l.get("nomStructure",""),
        "plan": l.get("plan","trial"), "planNom": p["nom"], "actif": actif,
        "dateExpiration": l.get("dateExpiration"), "joursRestants": j,
        "fonctionnalites": l.get("fonctionnalites", p["fonctionnalites"]),
        "maxCadres": l.get("maxCadres", p["max_cadres"]),
        "maxMembres": l.get("maxMembres", p.get("max_membres",9999)),
        "maxCreneaux": l.get("maxCreneaux", p.get("max_creneaux",9999)),
        "stripeCustomerId": l.get("stripeCustomerId"),
        "stripeSubscriptionId": l.get("stripeSubscriptionId"),
        "message": msg
    }

def verifier_admin():
    return request.headers.get("Authorization","").replace("Bearer ","") == ADMIN_TOKEN

# ── Routes publiques ──────────────────────────────────────

@app.route("/", methods=["GET","HEAD"])
def index():
    return jsonify({"service":"ManagerPresence License Server","status":"ok","version":"2.0.0","timestamp":datetime.now().isoformat()})

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status":"ok","timestamp":datetime.now().isoformat()})

@app.route("/licence/<pid>", methods=["GET"])
def get_licence(pid):
    l = charger_licence(pid)
    if l is None:
        l = creer_licence_trial(pid, request.args.get("nom",""))
        sauvegarder_licence(pid, l)
    return jsonify(formater_licence_response(l))

@app.route("/licence/<pid>/code", methods=["POST"])
def activer_code(pid):
    data = request.get_json() or {}
    code = data.get("code","").strip().upper()
    if not code: return jsonify({"error":"Code manquant"}), 400
    ci = charger_code(code)
    if ci is None: return jsonify({"error":"Code invalide"}), 404
    if ci.get("utilise"): return jsonify({"error":"Code déjà utilisé"}), 400
    l = charger_licence(pid) or creer_licence_trial(pid)
    tc = CODE_TYPES.get(ci.get("type"),{})
    if tc.get("plan"):
        pc = PLANS[tc["plan"]]
        l.update({"plan":tc["plan"],"fonctionnalites":pc["fonctionnalites"],"maxCadres":pc["max_cadres"],
                  "maxMembres":pc.get("max_membres",9999),"maxCreneaux":pc.get("max_creneaux",9999),
                  "dateExpiration":(datetime.now()+timedelta(days=tc["jours"])).isoformat()})
    else:
        try:
            d = datetime.fromisoformat(l["dateExpiration"].replace("Z","+00:00"))
            if d.tzinfo: d = d.replace(tzinfo=None)
        except: d = datetime.now()
        if d < datetime.now(): d = datetime.now()
        l["dateExpiration"] = (d + timedelta(days=tc["jours"])).isoformat()
    l["actif"] = True; l["message"] = f"Code {code} activé !"
    ci.update({"utilise":True,"utilise_par":pid,"utilise_le":datetime.now().isoformat()})
    sauvegarder_licence(pid, l); sauvegarder_code(code, ci)
    envoyer_notification("🎟️ Code activé", f"Code: {code}\nType: {ci.get('type')}\nProject: {pid}")
    return jsonify({"success":True,"message":f"Plan : {PLANS[l['plan']]['nom']}.","licence":formater_licence_response(l)})

# ── Routes Stripe ─────────────────────────────────────────

@app.route("/stripe/prices", methods=["GET"])
def stripe_prices():
    return jsonify({
        "standard":{"monthly":{"id":STRIPE_PRICES["standard_monthly"],"price":4.90,"currency":"eur"},
                    "yearly": {"id":STRIPE_PRICES["standard_yearly"], "price":49.90,"currency":"eur"}},
        "premium": {"monthly":{"id":STRIPE_PRICES["premium_monthly"], "price":9.99,"currency":"eur"},
                    "yearly": {"id":STRIPE_PRICES["premium_yearly"],  "price":99.99,"currency":"eur"}},
        "publicKey": STRIPE_PUBLIC_KEY
    })

@app.route("/stripe/checkout", methods=["POST"])
def stripe_checkout():
    data = request.get_json() or {}
    pid      = data.get("projectId","").strip()
    price_id = data.get("priceId","").strip()
    email    = data.get("email","").strip()
    nom      = data.get("nomStructure","").strip()
    if not pid or not price_id: return jsonify({"error":"projectId et priceId requis"}), 400
    if price_id not in STRIPE_PRICES.values(): return jsonify({"error":"Prix invalide"}), 400
    l = charger_licence(pid)
    if l is None:
        l = creer_licence_trial(pid, nom); sauvegarder_licence(pid, l)
    try:
        cid = l.get("stripeCustomerId")
        if not cid:
            c = stripe.Customer.create(email=email or None, metadata={"projectId":pid,"nomStructure":nom or l.get("nomStructure","")})
            cid = c.id; l["stripeCustomerId"] = cid; sauvegarder_licence(pid, l)
        session = stripe.checkout.Session.create(
            customer=cid, payment_method_types=["card"],
            line_items=[{"price":price_id,"quantity":1}], mode="subscription",
            success_url=f"{PWA_SUCCESS_URL}?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=PWA_CANCEL_URL,
            metadata={"projectId":pid},
            subscription_data={"metadata":{"projectId":pid}},
            allow_promotion_codes=True
        )
        return jsonify({"success":True,"sessionId":session.id,"url":session.url})
    except stripe.error.StripeError as e:
        print(f"Stripe checkout: {e}"); return jsonify({"error":str(e)}), 500

@app.route("/stripe/portal", methods=["POST"])
def stripe_portal():
    data = request.get_json() or {}
    pid  = data.get("projectId","").strip()
    if not pid: return jsonify({"error":"projectId requis"}), 400
    l = charger_licence(pid)
    if not l: return jsonify({"error":"Licence non trouvée"}), 404
    cid = l.get("stripeCustomerId")
    if not cid: return jsonify({"error":"Aucun abonnement Stripe associé"}), 400
    try:
        ps = stripe.billing_portal.Session.create(customer=cid, return_url=PWA_CANCEL_URL)
        return jsonify({"success":True,"url":ps.url})
    except stripe.error.StripeError as e:
        print(f"Stripe portal: {e}"); return jsonify({"error":str(e)}), 500

@app.route("/stripe/webhook", methods=["POST"])
def stripe_webhook():
    payload = request.get_data(as_text=True)
    sig     = request.headers.get("Stripe-Signature","")
    if STRIPE_WEBHOOK_SECRET:
        try:
            event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
        except (ValueError, stripe.error.SignatureVerificationError) as e:
            return jsonify({"error":str(e)}), 400
    else:
        event = json.loads(payload)
    et  = event.get("type","")
    obj = event.get("data",{}).get("object",{})
    print(f"[STRIPE] {et}")

    if et == "checkout.session.completed":
        pid = obj.get("metadata",{}).get("projectId")
        if pid and obj.get("subscription"):
            _sub_created(pid, obj["subscription"], obj.get("customer"))

    elif et == "customer.subscription.created":
        pid = obj.get("metadata",{}).get("projectId")
        if pid: _sub_created(pid, obj.get("id"), obj.get("customer"))

    elif et == "customer.subscription.updated":
        pid = obj.get("metadata",{}).get("projectId")
        if pid: _sub_updated(pid, obj)

    elif et == "customer.subscription.deleted":
        pid = obj.get("metadata",{}).get("projectId")
        if pid: _sub_cancelled(pid)

    elif et == "invoice.payment_succeeded":
        sid = obj.get("subscription")
        if sid:
            try:
                sub = stripe.Subscription.retrieve(sid)
                pid = sub.get("metadata",{}).get("projectId")
                if pid: _payment_ok(pid, sub)
            except Exception as e: print(f"invoice ok: {e}")

    elif et == "invoice.payment_failed":
        sid   = obj.get("subscription")
        email = obj.get("customer_email","")
        if sid:
            try:
                sub = stripe.Subscription.retrieve(sid)
                pid = sub.get("metadata",{}).get("projectId")
                if pid: _payment_fail(pid, email)
            except Exception as e: print(f"invoice fail: {e}")

    return jsonify({"received":True})

def _sub_created(pid, sid, cid):
    try:
        sub      = stripe.Subscription.retrieve(sid)
        price_id = sub["items"]["data"][0]["price"]["id"]
        interval = sub["items"]["data"][0]["price"]["recurring"]["interval"]
        plan     = "premium" if price_id in [STRIPE_PRICES["premium_monthly"],STRIPE_PRICES["premium_yearly"]] else "standard"
        jours    = 365 if interval == "year" else 31
        l = charger_licence(pid)
        if l:
            pc = PLANS[plan]
            l.update({"plan":plan,"fonctionnalites":pc["fonctionnalites"],"maxCadres":pc["max_cadres"],
                      "maxMembres":pc.get("max_membres",9999),"maxCreneaux":pc.get("max_creneaux",9999),
                      "dateExpiration":(datetime.now()+timedelta(days=jours)).isoformat(),
                      "actif":True,"stripeCustomerId":cid,"stripeSubscriptionId":sid,
                      "message":f"Merci ! Abonnement {pc['nom']} actif."})
            sauvegarder_licence(pid, l)
            envoyer_notification("💳 Nouvel abonnement", f"Project: {pid}\nPlan: {plan}\nSub: {sid}")
    except Exception as e: print(f"_sub_created: {e}")

def _sub_updated(pid, sub):
    try:
        price_id = sub["items"]["data"][0]["price"]["id"]
        status   = sub.get("status")
        plan     = "premium" if price_id in [STRIPE_PRICES["premium_monthly"],STRIPE_PRICES["premium_yearly"]] else "standard"
        l = charger_licence(pid)
        if l:
            if status == "active":
                pc = PLANS[plan]
                l.update({"plan":plan,"fonctionnalites":pc["fonctionnalites"],"maxCadres":pc["max_cadres"],
                          "maxMembres":pc.get("max_membres",9999),"maxCreneaux":pc.get("max_creneaux",9999),"actif":True})
                pe = sub.get("current_period_end")
                if pe: l["dateExpiration"] = datetime.fromtimestamp(pe).isoformat()
            elif status in ["past_due","unpaid"]:
                l["message"] = "⚠️ Problème de paiement — Mettez à jour votre carte."
            elif status == "canceled":
                l["message"] = "Abonnement annulé. Actif jusqu'à la fin de la période."
            sauvegarder_licence(pid, l)
    except Exception as e: print(f"_sub_updated: {e}")

def _sub_cancelled(pid):
    l = charger_licence(pid)
    if l:
        l.update({"stripeSubscriptionId":None,"message":f"Abonnement annulé. Actif jusqu'au {l.get('dateExpiration','')[:10]}."})
        sauvegarder_licence(pid, l)
        envoyer_notification("❌ Abonnement annulé", f"Project: {pid}\nStructure: {l.get('nomStructure','N/A')}")

def _payment_ok(pid, sub):
    l = charger_licence(pid)
    if l:
        pe = sub.get("current_period_end")
        if pe: l["dateExpiration"] = datetime.fromtimestamp(pe).isoformat()
        l.update({"actif":True,"message":"Abonnement renouvelé — merci !"})
        sauvegarder_licence(pid, l)

def _payment_fail(pid, email):
    l = charger_licence(pid)
    if l:
        l["message"] = "⚠️ Échec du paiement. Mettez à jour votre carte via le portail client."
        sauvegarder_licence(pid, l)
        envoyer_notification("⚠️ Paiement échoué", f"Project: {pid}\nEmail: {email}")

# ── Routes PWA ────────────────────────────────────────────

@app.route("/pwa/generate", methods=["POST"])
def pwa_generate():
    data = request.get_json() or {}
    for f in ["projectId","code","firebaseConfig"]:
        if not data.get(f): return jsonify({"error":f"Champ manquant: {f}"}), 400
    pid  = data["projectId"]; code = data["code"].upper()
    l    = charger_licence(pid)
    if l:
        if l.get("plan") == "standard": return jsonify({"error":"PWA nécessite Trial ou Premium"}), 403
        if calculer_jours_restants(l.get("dateExpiration","")) <= 0: return jsonify({"error":"Licence expirée"}), 403
    now = datetime.now(); exp = now + timedelta(seconds=PWA_CODE_VALIDITY)
    exp_ms = int(exp.timestamp()*1000)
    pwa = {"projectId":pid,"code":code,"generatedBy":data.get("generatedBy","Admin"),
           "clubName":data.get("clubName",""),"firebaseConfig":data["firebaseConfig"],
           "createdAt":now.isoformat(),"expiresAt":exp_ms,"used":False}
    if not sauvegarder_pwa_code(code, pwa): return jsonify({"error":"Erreur sauvegarde"}), 500
    nettoyer_codes_expires()
    return jsonify({"success":True,"code":code,"expiresAt":exp_ms,"validitySeconds":PWA_CODE_VALIDITY}), 201

@app.route("/pwa/verify", methods=["POST"])
def pwa_verify():
    data = request.get_json() or {}
    code = data.get("code","").strip().upper()
    if not code: return jsonify({"error":"Code manquant"}), 400
    pwa = charger_pwa_code(code)
    if pwa is None: return jsonify({"error":"Code invalide ou expiré"}), 404
    now_ms = int(datetime.now().timestamp()*1000)
    if now_ms > pwa.get("expiresAt",0):
        supprimer_pwa_code(code); return jsonify({"error":"Code expiré"}), 410
    if pwa.get("used"): return jsonify({"error":"Code déjà utilisé"}), 400
    pwa.update({"used":True,"usedAt":datetime.now().isoformat()})
    sauvegarder_pwa_code(code, pwa)
    pid = pwa.get("projectId","")
    l   = charger_licence(pid)
    return jsonify({"success":True,"projectId":pid,"clubName":pwa.get("clubName",""),
                    "firebaseConfig":pwa.get("firebaseConfig",{}),"generatedBy":pwa.get("generatedBy",""),
                    "licence":formater_licence_response(l) if l else None})

@app.route("/pwa/status/<code>", methods=["GET"])
def pwa_status(code):
    code = code.upper(); pwa = charger_pwa_code(code)
    if pwa is None: return jsonify({"exists":False,"status":"not_found"})
    now_ms = int(datetime.now().timestamp()*1000); exp = pwa.get("expiresAt",0)
    if now_ms > exp: return jsonify({"exists":True,"status":"expired"})
    if pwa.get("used"): return jsonify({"exists":True,"status":"used","usedAt":pwa.get("usedAt","")})
    return jsonify({"exists":True,"status":"active","remainingSeconds":int((exp-now_ms)/1000)})

# ── Routes Admin ──────────────────────────────────────────

@app.route("/admin/liste", methods=["GET"])
def admin_liste():
    if not verifier_admin(): return jsonify({"error":"Non autorisé"}), 401
    liste = sorted([formater_licence_response(l) for l in charger_licences().values()],
                   key=lambda x: x.get("dateExpiration",""), reverse=True)
    return jsonify({"total":len(liste),"licences":liste})

@app.route("/admin/gencode", methods=["POST"])
def admin_gencode():
    if not verifier_admin(): return jsonify({"error":"Non autorisé"}), 401
    data = request.get_json() or {}
    ct   = data.get("type","").upper()
    if ct not in CODE_TYPES: return jsonify({"error":f"Types valides: {list(CODE_TYPES.keys())}"}), 400
    conf = CODE_TYPES[ct]; codes = charger_codes()
    nc   = generer_code(conf["prefixe"])
    while nc in codes: nc = generer_code(conf["prefixe"])
    sauvegarder_code(nc, {"type":ct,"cree_le":datetime.now().isoformat(),"utilise":False})
    return jsonify({"code":nc,"type":ct,"effet":f"{conf.get('plan','Prolongation')} — {conf['jours']} jours"})

@app.route("/admin/codes", methods=["GET"])
def admin_codes():
    if not verifier_admin(): return jsonify({"error":"Non autorisé"}), 401
    codes = charger_codes()
    liste = sorted([{"code":c,**i} for c,i in codes.items()], key=lambda x: x.get("cree_le",""), reverse=True)
    return jsonify({"total":len(liste),"codes":liste})

@app.route("/admin/pwa-codes", methods=["GET"])
def admin_pwa_codes():
    if not verifier_admin(): return jsonify({"error":"Non autorisé"}), 401
    try:
        now_ms = int(datetime.now().timestamp()*1000); codes = []
        for doc in db.collection("pwa_codes").stream():
            d = doc.to_dict(); exp = d.get("expiresAt",0)
            status = "expired" if now_ms > exp else ("used" if d.get("used") else "active")
            codes.append({"code":doc.id,"projectId":d.get("projectId",""),"clubName":d.get("clubName",""),
                          "generatedBy":d.get("generatedBy",""),"createdAt":d.get("createdAt",""),"status":status})
        codes.sort(key=lambda x: x.get("createdAt",""), reverse=True)
        return jsonify({"total":len(codes),"codes":codes})
    except Exception as e: return jsonify({"error":str(e)}), 500

@app.route("/licence/<pid>", methods=["POST"])
def admin_update_licence(pid):
    if not verifier_admin(): return jsonify({"error":"Non autorisé"}), 401
    data = request.get_json() or {}; l = charger_licence(pid)
    if l is None: return jsonify({"error":"Licence non trouvée"}), 404
    if "plan" in data and data["plan"] in PLANS:
        pc = PLANS[data["plan"]]
        l.update({"plan":data["plan"],"fonctionnalites":pc["fonctionnalites"],
                  "maxCadres":pc["max_cadres"],"maxMembres":pc.get("max_membres",9999),"maxCreneaux":pc.get("max_creneaux",9999)})
    for f in ["actif","dateExpiration","nomStructure","message"]:
        if f in data: l[f] = data[f]
    if "joursSupplementaires" in data:
        try:
            d = datetime.fromisoformat(l["dateExpiration"].replace("Z","+00:00"))
            if d.tzinfo: d = d.replace(tzinfo=None)
        except: d = datetime.now()
        if d < datetime.now(): d = datetime.now()
        l["dateExpiration"] = (d + timedelta(days=int(data["joursSupplementaires"]))).isoformat()
    sauvegarder_licence(pid, l)
    return jsonify({"success":True,"licence":formater_licence_response(l)})

@app.route("/admin/licence/<pid>", methods=["PUT"])
def admin_edit_licence(pid):
    if not verifier_admin(): return jsonify({"error":"Non autorisé"}), 401
    data = request.get_json() or {}; l = charger_licence(pid)
    if l is None: return jsonify({"error":"Licence non trouvée"}), 404
    if "plan" in data and data["plan"] in PLANS:
        pc = PLANS[data["plan"]]
        l.update({"plan":data["plan"],"fonctionnalites":pc["fonctionnalites"]})
        if "maxCadres" not in data: l["maxCadres"] = pc["max_cadres"]
    if "duree" in data:
        l["dateExpiration"] = (datetime.now() + timedelta(days=int(data["duree"]))).isoformat()
        l["actif"] = True
    for f in ["maxCadres","nomStructure"]:
        if f in data: l[f] = data[f]
    sauvegarder_licence(pid, l)
    envoyer_notification("✏️ Licence modifiée", f"Project: {pid}\nPlan: {l.get('plan')}\nExpiration: {l.get('dateExpiration')}")
    return jsonify({"success":True,"licence":formater_licence_response(l)})

# ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
