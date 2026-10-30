"""
ManagerPresence - Mails d'absence envoyés depuis le Gmail de la structure

Le club autorise une seule chose : « envoyer des e-mails en votre nom » (scope gmail.send).
Cette autorisation passe par un client OAuth DISTINCT de celui de la création de structure :
le jeton de création (accès complet à Google Cloud) est révoqué en fin de création, et une
révocation Google retire toutes les autorisations d'un même client. Le jeton gmail.send, lui,
est gardé chiffré (Fernet) pour que n'importe quel cadre puisse déclencher l'envoi.

Preuve d'appartenance à la structure : un secret tiré au hasard par l'app à la première
autorisation, rangé dans la base Firestore de la structure (partagé par tous ses appareils).
Le serveur n'en garde que l'empreinte SHA-256.

Variables Render : GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, MAIL_TOKEN_KEY (clé Fernet).
Sans elles, la fonction est simplement indisponible (l'app garde l'envoi SMTP).
"""

import base64
import hashlib
import hmac
import html
import os
import re
import secrets
import threading
import time
import urllib.parse
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr

import requests as http_requests
from flask import Blueprint, jsonify, redirect, request

GMAIL_CLIENT_ID = os.environ.get("GMAIL_CLIENT_ID", "")
GMAIL_CLIENT_SECRET = os.environ.get("GMAIL_CLIENT_SECRET", "")
MAIL_TOKEN_KEY = os.environ.get("MAIL_TOKEN_KEY", "")

SCOPE_ENVOI = "https://www.googleapis.com/auth/gmail.send"
SCOPES_MAIL = ["openid", "https://www.googleapis.com/auth/userinfo.email", SCOPE_ENVOI]

DUREE_LIEN_SECONDES = 7 * 86400   # lien partageable valable 7 jours
MAX_PAR_APPEL = 60                # destinataires par validation d'appel
MAX_PAR_JOUR = 300                # mails par structure et par jour (garde-fou anti-abus)

_RE_EMAIL = re.compile(r"^[^@\s<>\"',;]+@[^@\s<>\"',;]+\.[A-Za-z]{2,}$")
_RE_INTERDIT = re.compile(r"https?:|www\.|://|[<>]", re.IGNORECASE)

# ============================================================
# TEXTES (pages web)
# ============================================================

_T = {
    "titre":          {"FR": "Mails d'absence automatiques", "EN": "Automatic absence emails", "ES": "Correos de ausencia automáticos", "DE": "Automatische Abwesenheits-E-Mails", "IT": "Email di assenza automatiche", "PT": "Emails de ausência automáticos"},
    "intro":          {"FR": "Quand un encadrant valide l'appel, les parents d'un membre absent sans avoir prévenu reçoivent un mail. Ce mail part de l'adresse Gmail que vous allez autoriser : les parents peuvent y répondre directement.",
                       "EN": "When a supervisor validates the roll call, the parents of a member who is absent without notice receive an email. It is sent from the Gmail address you are about to authorize, so parents can reply directly.",
                       "ES": "Cuando un responsable valida la lista, los padres de un miembro ausente sin aviso reciben un correo. Se envía desde la dirección Gmail que va a autorizar: los padres pueden responder directamente.",
                       "DE": "Wenn eine Betreuungsperson die Anwesenheit bestätigt, erhalten die Eltern eines unentschuldigt fehlenden Mitglieds eine E-Mail. Sie wird von der Gmail-Adresse gesendet, die Sie gleich autorisieren – die Eltern können direkt antworten.",
                       "IT": "Quando un responsabile convalida l'appello, i genitori di un membro assente senza preavviso ricevono un'email. Parte dall'indirizzo Gmail che state per autorizzare: i genitori possono rispondere direttamente.",
                       "PT": "Quando um responsável valida a chamada, os pais de um membro ausente sem aviso recebem um email. É enviado a partir do endereço Gmail que vai autorizar: os pais podem responder diretamente."},
    "structure":      {"FR": "Structure", "EN": "Organization", "ES": "Estructura", "DE": "Einrichtung", "IT": "Struttura", "PT": "Estrutura"},
    "google_demande": {"FR": "Google va vous demander d'autoriser ManagerPresence à « Envoyer des e-mails en votre nom ».",
                       "EN": "Google will ask you to allow ManagerPresence to “Send email on your behalf”.",
                       "ES": "Google le pedirá que autorice a ManagerPresence a «Enviar correos en su nombre».",
                       "DE": "Google bittet Sie, ManagerPresence zu erlauben, „E-Mails in Ihrem Namen zu senden“.",
                       "IT": "Google vi chiederà di autorizzare ManagerPresence a «Inviare email per vostro conto».",
                       "PT": "A Google vai pedir que autorize o ManagerPresence a «Enviar emails em seu nome»."},
    "garantie1":      {"FR": "ManagerPresence ne peut ni lire ni supprimer vos mails : seulement en envoyer.", "EN": "ManagerPresence can neither read nor delete your emails: it can only send.", "ES": "ManagerPresence no puede leer ni borrar sus correos: solo enviarlos.", "DE": "ManagerPresence kann Ihre E-Mails weder lesen noch löschen – nur senden.", "IT": "ManagerPresence non può né leggere né cancellare le vostre email: può solo inviarle.", "PT": "O ManagerPresence não pode ler nem apagar os seus emails: só enviá-los."},
    "garantie2":      {"FR": "Seuls les mails d'absence partent, vers les adresses enregistrées dans les fiches des membres.", "EN": "Only absence emails are sent, to the addresses saved in the members' records.", "ES": "Solo se envían los correos de ausencia, a las direcciones de las fichas de los miembros.", "DE": "Es werden nur Abwesenheits-E-Mails gesendet, an die in den Mitgliederdaten hinterlegten Adressen.", "IT": "Partono solo le email di assenza, verso gli indirizzi registrati nelle schede dei membri.", "PT": "Só são enviados os emails de ausência, para os endereços registados nas fichas dos membros."},
    "garantie3":      {"FR": "Vous pouvez retirer cette autorisation à tout moment, dans l'application ou dans votre compte Google.", "EN": "You can withdraw this permission at any time, in the app or in your Google account.", "ES": "Puede retirar esta autorización en cualquier momento, en la aplicación o en su cuenta de Google.", "DE": "Sie können diese Berechtigung jederzeit in der App oder in Ihrem Google-Konto widerrufen.", "IT": "Potete revocare questa autorizzazione in qualsiasi momento, nell'app o nel vostro account Google.", "PT": "Pode retirar esta autorização a qualquer momento, na aplicação ou na sua conta Google."},
    "compte_conseil": {"FR": "Connectez-vous avec le Gmail officiel de la structure : c'est lui qui apparaîtra comme expéditeur.", "EN": "Sign in with the organization's official Gmail: it will appear as the sender.", "ES": "Inicie sesión con el Gmail oficial de la estructura: aparecerá como remitente.", "DE": "Melden Sie sich mit dem offiziellen Gmail der Einrichtung an: Es erscheint als Absender.", "IT": "Accedete con il Gmail ufficiale della struttura: apparirà come mittente.", "PT": "Inicie sessão com o Gmail oficial da estrutura: será ele o remetente."},
    "bouton":         {"FR": "Autoriser avec Google", "EN": "Authorize with Google", "ES": "Autorizar con Google", "DE": "Mit Google autorisieren", "IT": "Autorizza con Google", "PT": "Autorizar com a Google"},
    "ok_titre":       {"FR": "C'est fait !", "EN": "All set!", "ES": "¡Listo!", "DE": "Erledigt!", "IT": "Fatto!", "PT": "Está feito!"},
    "ok_corps":       {"FR": "Les mails d'absence de <strong>{club}</strong> partiront désormais depuis <strong>{email}</strong>.", "EN": "Absence emails for <strong>{club}</strong> will now be sent from <strong>{email}</strong>.", "ES": "Los correos de ausencia de <strong>{club}</strong> se enviarán desde <strong>{email}</strong>.", "DE": "Abwesenheits-E-Mails von <strong>{club}</strong> werden ab jetzt von <strong>{email}</strong> gesendet.", "IT": "Le email di assenza di <strong>{club}</strong> partiranno ora da <strong>{email}</strong>.", "PT": "Os emails de ausência de <strong>{club}</strong> passam a ser enviados de <strong>{email}</strong>."},
    "ok_fermer":      {"FR": "Vous pouvez fermer cette page.", "EN": "You can close this page.", "ES": "Puede cerrar esta página.", "DE": "Sie können diese Seite schließen.", "IT": "Potete chiudere questa pagina.", "PT": "Pode fechar esta página."},
    "err_lien":       {"FR": "Ce lien n'est plus valable. Demandez-en un nouveau depuis l'application ManagerPresence.", "EN": "This link is no longer valid. Request a new one from the ManagerPresence app.", "ES": "Este enlace ya no es válido. Pida uno nuevo desde la aplicación ManagerPresence.", "DE": "Dieser Link ist nicht mehr gültig. Fordern Sie in der ManagerPresence-App einen neuen an.", "IT": "Questo link non è più valido. Richiedetene uno nuovo dall'app ManagerPresence.", "PT": "Este link já não é válido. Peça um novo na aplicação ManagerPresence."},
    "err_refus":      {"FR": "Autorisation non accordée. Rien n'a été modifié : vous pouvez réessayer avec le même lien.", "EN": "Permission not granted. Nothing was changed: you can try again with the same link.", "ES": "Autorización no concedida. No se ha cambiado nada: puede reintentar con el mismo enlace.", "DE": "Berechtigung nicht erteilt. Nichts wurde geändert: Sie können es mit demselben Link erneut versuchen.", "IT": "Autorizzazione non concessa. Nulla è stato modificato: potete riprovare con lo stesso link.", "PT": "Autorização não concedida. Nada foi alterado: pode tentar de novo com o mesmo link."},
    "err_case":       {"FR": "La case « Envoyer des e-mails en votre nom » n'était pas cochée. Recommencez et cochez-la.", "EN": "The “Send email on your behalf” box was not ticked. Try again and tick it.", "ES": "La casilla «Enviar correos en su nombre» no estaba marcada. Vuelva a intentarlo y márquela.", "DE": "Das Kästchen „E-Mails in Ihrem Namen senden“ war nicht angehakt. Bitte erneut versuchen und anhaken.", "IT": "La casella «Inviare email per vostro conto» non era selezionata. Riprovate e selezionatela.", "PT": "A caixa «Enviar emails em seu nome» não estava marcada. Tente de novo e marque-a."},
    "err_technique":  {"FR": "Une erreur technique est survenue. Réessayez dans quelques minutes.", "EN": "A technical error occurred. Please try again in a few minutes.", "ES": "Se ha producido un error técnico. Inténtelo de nuevo en unos minutos.", "DE": "Ein technischer Fehler ist aufgetreten. Bitte in einigen Minuten erneut versuchen.", "IT": "Si è verificato un errore tecnico. Riprovate tra qualche minuto.", "PT": "Ocorreu um erro técnico. Tente de novo dentro de alguns minutos."},
}


def _t(lang, cle):
    entree = _T.get(cle, {})
    return entree.get(lang, entree.get("FR", cle))


def _page(titre, corps, couleur="#1565C0"):
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(titre)} — ManagerPresence</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:Arial,sans-serif;background:#F5F5F5;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px;color:#333}}
.card{{background:#fff;border-radius:16px;padding:32px 24px;max-width:460px;width:100%;box-shadow:0 4px 24px rgba(0,0,0,.08);line-height:1.5}}
h1{{color:{couleur};font-size:22px;margin-bottom:16px;text-align:center}}
p{{margin:12px 0}} ul{{margin:12px 0 12px 20px}} li{{margin:6px 0}}
.club{{background:#E3F2FD;border-radius:8px;padding:10px 14px;text-align:center}}
.conseil{{background:#FFF8E1;border-radius:8px;padding:10px 14px;font-size:14px}}
.btn{{display:block;background:#1565C0;color:#fff;text-decoration:none;border-radius:8px;padding:16px;font-size:16px;font-weight:bold;text-align:center;margin-top:20px}}
</style></head><body><div class="card">{corps}</div></body></html>"""


# ============================================================
# OUTILS
# ============================================================

def _empreinte(secret):
    return hashlib.sha256((secret or "").encode("utf-8")).hexdigest()


def _fernet():
    from cryptography.fernet import Fernet
    return Fernet(MAIL_TOKEN_KEY.encode())


def _chiffrer(texte):
    return _fernet().encrypt(texte.encode()).decode()


def _dechiffrer(texte):
    return _fernet().decrypt(texte.encode()).decode()


def _propre(valeur, maxi=80):
    """Texte inséré dans un mail : une ligne, court, sans lien ni balise (anti-spam)."""
    v = re.sub(r"[\x00-\x1f\x7f]", " ", str(valeur or "")).strip()[:maxi]
    return "" if _RE_INTERDIT.search(v) else v


def _revoquer(refresh_token):
    try:
        http_requests.post("https://oauth2.googleapis.com/revoke", params={"token": refresh_token}, timeout=15)
    except Exception as e:
        print(f"[MAIL] ⚠️ Révocation: {e}")


def creer_blueprint(db, server_base_url, admin_token):
    bp = Blueprint("mail_absences", __name__)
    redirect_uri = f"{server_base_url}/mail/oauth/callback"
    jetons_acces = {}          # project_id -> (access_token, expire_a)
    verrou = threading.Lock()

    def disponible():
        return bool(GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET and MAIL_TOKEN_KEY)

    def autorisation(project_id):
        doc = db.collection("mail_autorisations").document(project_id).get()
        return doc.to_dict() if doc.exists else None

    def verifier_secret(project_id, secret):
        """None si OK, sinon (réponse JSON, code)."""
        if not project_id or not secret or len(secret) < 32:
            return jsonify({"erreur": "parametres"}), 400
        auto = autorisation(project_id)
        if auto and not hmac.compare_digest(auto.get("secret_hash", ""), _empreinte(secret)):
            return jsonify({"erreur": "secret_incorrect"}), 403
        return None

    def jeton_acces(project_id, auto):
        with verrou:
            j = jetons_acces.get(project_id)
            if j and j[1] > time.time() + 60:
                return j[0]
        r = http_requests.post("https://oauth2.googleapis.com/token", data={
            "client_id": GMAIL_CLIENT_ID,
            "client_secret": GMAIL_CLIENT_SECRET,
            "refresh_token": _dechiffrer(auto["refresh_chiffre"]),
            "grant_type": "refresh_token",
        }, timeout=20)
        data = r.json()
        if "access_token" not in data:
            if data.get("error") == "invalid_grant":
                # Autorisation retirée depuis le compte Google : on l'oublie
                db.collection("mail_autorisations").document(project_id).delete()
                raise PermissionError("autorisation_retiree")
            raise RuntimeError(data.get("error_description") or data.get("error") or "token")
        with verrou:
            jetons_acces[project_id] = (data["access_token"], time.time() + int(data.get("expires_in", 3600)))
        return data["access_token"]

    def url_google(lien_token, lien, choisir_compte):
        params = {
            "client_id": GMAIL_CLIENT_ID,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(SCOPES_MAIL),
            "access_type": "offline",
            "include_granted_scopes": "false",
            "prompt": "consent select_account" if choisir_compte else "consent",
            "state": lien_token,
        }
        if lien.get("hint") and not choisir_compte:
            params["login_hint"] = lien["hint"]
        return "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params)

    def charger_lien(lien_token):
        if not lien_token or len(lien_token) > 100:
            return None
        doc = db.collection("mail_liens").document(lien_token).get()
        if not doc.exists:
            return None
        lien = doc.to_dict()
        if lien.get("utilise") or int(time.time()) > lien.get("expire_a", 0):
            return None
        return lien

    # ---------------------------------------------------------- API de l'app

    @bp.route("/mail/lien", methods=["POST"])
    def mail_lien():
        """Prépare une autorisation : renvoie la page à partager et l'URL Google directe."""
        if not disponible():
            return jsonify({"erreur": "indisponible"}), 503
        d = request.get_json(silent=True) or {}
        project_id = str(d.get("project_id", "")).strip()
        secret = str(d.get("secret", ""))
        refus = verifier_secret(project_id, secret)
        if refus:
            return refus
        hint = str(d.get("hint", "")).strip()
        lien_token = secrets.token_urlsafe(32)
        db.collection("mail_liens").document(lien_token).set({
            "project_id": project_id,
            "secret_hash": _empreinte(secret),
            "club_name": _propre(d.get("club_name", "")) or project_id,
            "lang": str(d.get("lang", "FR")).upper()[:2],
            "hint": hint if _RE_EMAIL.match(hint) else "",
            "cree_a": int(time.time()),
            "expire_a": int(time.time()) + DUREE_LIEN_SECONDES,
        })
        page = f"{server_base_url}/mail/autoriser/{lien_token}"
        return jsonify({"page": page, "google": f"{page}/google", "google_autre_compte": f"{page}/google?autre=1"})

    @bp.route("/mail/statut", methods=["POST"])
    def mail_statut():
        if not disponible():
            return jsonify({"disponible": False, "actif": False})
        d = request.get_json(silent=True) or {}
        project_id = str(d.get("project_id", "")).strip()
        refus = verifier_secret(project_id, str(d.get("secret", "")))
        if refus:
            return refus
        auto = autorisation(project_id)
        return jsonify({"disponible": True, "actif": bool(auto), "email": (auto or {}).get("email", "")})

    @bp.route("/mail/retirer", methods=["POST"])
    def mail_retirer():
        d = request.get_json(silent=True) or {}
        project_id = str(d.get("project_id", "")).strip()
        refus = verifier_secret(project_id, str(d.get("secret", "")))
        if refus:
            return refus
        auto = autorisation(project_id)
        if auto:
            try:
                _revoquer(_dechiffrer(auto["refresh_chiffre"]))
            except Exception as e:
                print(f"[MAIL] ⚠️ Retrait {project_id}: {e}")
            db.collection("mail_autorisations").document(project_id).delete()
            with verrou:
                jetons_acces.pop(project_id, None)
        return jsonify({"ok": True})

    @bp.route("/mail/absences", methods=["POST"])
    def mail_absences():
        """Envoie les mails d'absence d'une séance depuis le Gmail autorisé de la structure."""
        if not disponible():
            return jsonify({"erreur": "indisponible"}), 503
        d = request.get_json(silent=True) or {}
        project_id = str(d.get("project_id", "")).strip()
        secret = str(d.get("secret", ""))
        if not project_id or not secret:
            return jsonify({"erreur": "parametres"}), 400
        auto = autorisation(project_id)
        if not auto:
            return jsonify({"erreur": "non_autorise"}), 404
        if not hmac.compare_digest(auto.get("secret_hash", ""), _empreinte(secret)):
            return jsonify({"erreur": "secret_incorrect"}), 403

        eleves = d.get("eleves") or []
        if not isinstance(eleves, list) or not eleves:
            return jsonify({"envoyes": 0, "echecs": []})
        if len(eleves) > MAX_PAR_APPEL:
            return jsonify({"erreur": "trop_de_destinataires"}), 400

        jour = datetime.utcnow().strftime("%Y-%m-%d")
        deja = auto.get("envoyes", 0) if auto.get("jour") == jour else 0
        if deja + len(eleves) > MAX_PAR_JOUR:
            return jsonify({"erreur": "quota_jour"}), 429

        club = _propre(d.get("club_name", "")) or auto.get("club_name", "")
        creneau = _propre(d.get("creneau", "")) or "Séance"
        date_seance = _propre(d.get("date", ""), 20)

        try:
            acces = jeton_acces(project_id, auto)
        except PermissionError:
            return jsonify({"erreur": "autorisation_retiree"}), 401
        except Exception as e:
            print(f"[MAIL] ❌ Jeton {project_id}: {e}")
            return jsonify({"erreur": "google"}), 502

        envoyes, echecs = 0, []
        for e in eleves:
            prenom, nom = _propre(e.get("prenom", ""), 60), _propre(e.get("nom", ""), 60)
            adresses = [a.strip() for a in str(e.get("email", "")).split(",") if a.strip()][:3]
            if not (prenom or nom) or not adresses or not all(_RE_EMAIL.match(a) for a in adresses):
                echecs.append(f"{prenom} {nom}".strip() or "?")
                continue
            msg = EmailMessage()
            msg["From"] = formataddr((club, auto["email"])) if club else auto["email"]
            msg["To"] = ", ".join(adresses)
            msg["Subject"] = f"Absence non excusée - {prenom} {nom}".strip()
            msg.set_content(
                "Bonjour,\n\n"
                f"Nous vous informons que {prenom} {nom} ne s'est pas présenté(e) "
                f"à la séance du {date_seance} ({creneau}).\n\n"
                "Par souci de sécurité, toute absence non excusée fera l'objet d'un message "
                "systématique de notre part. Vous pouvez éviter ces courriers en nous prévenant "
                "avant le début de la séance (WhatsApp ou SMS).\n\n"
                "Verticalement vôtre,\n"
                f"L'équipe {club}\n"
            )
            raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
            try:
                r = http_requests.post("https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
                                       headers={"Authorization": f"Bearer {acces}"},
                                       json={"raw": raw}, timeout=20)
                if r.status_code == 200:
                    envoyes += 1
                else:
                    print(f"[MAIL] ❌ Gmail {r.status_code} ({project_id}): {r.text[:200]}")
                    echecs.append(f"{prenom} {nom}".strip())
            except Exception as ex:
                print(f"[MAIL] ❌ Envoi ({project_id}): {ex}")
                echecs.append(f"{prenom} {nom}".strip())

        db.collection("mail_autorisations").document(project_id).update(
            {"jour": jour, "envoyes": deja + envoyes, "dernier_envoi": int(time.time())})
        print(f"[MAIL] 📧 {project_id}: {envoyes} envoyé(s), {len(echecs)} échec(s)")
        return jsonify({"envoyes": envoyes, "echecs": echecs})

    # ---------------------------------------------------------- Pages web

    @bp.route("/mail/autoriser/<lien_token>", methods=["GET"])
    def page_autoriser(lien_token):
        """Page d'explication : celle qu'on ouvre depuis le lien partagé."""
        lien = charger_lien(lien_token)
        lang = (lien or {}).get("lang", "FR")
        if not lien:
            return _page("ManagerPresence", f"<h1>⏱️</h1><p>{_t(lang, 'err_lien')}</p>", "#C62828"), 404
        club = html.escape(lien.get("club_name", ""))
        corps = f"""<h1>📧 {_t(lang, 'titre')}</h1>
<p class="club">{_t(lang, 'structure')} : <strong>{club}</strong></p>
<p>{_t(lang, 'intro')}</p>
<p>{_t(lang, 'google_demande')}</p>
<ul><li>{_t(lang, 'garantie1')}</li><li>{_t(lang, 'garantie2')}</li><li>{_t(lang, 'garantie3')}</li></ul>
<p class="conseil">💡 {_t(lang, 'compte_conseil')}</p>
<a class="btn" href="/mail/autoriser/{lien_token}/google?autre=1">{_t(lang, 'bouton')}</a>"""
        return _page(_t(lang, "titre"), corps)

    @bp.route("/mail/autoriser/<lien_token>/google", methods=["GET"])
    def vers_google(lien_token):
        lien = charger_lien(lien_token)
        if not lien or not disponible():
            lang = (lien or {}).get("lang", "FR")
            return _page("ManagerPresence", f"<h1>⏱️</h1><p>{_t(lang, 'err_lien')}</p>", "#C62828"), 404
        return redirect(url_google(lien_token, lien, request.args.get("autre") == "1"))

    @bp.route("/mail/oauth/callback", methods=["GET"])
    def oauth_callback():
        lien_token = request.args.get("state", "")
        lien = charger_lien(lien_token)
        lang = (lien or {}).get("lang", "FR")
        if not lien:
            return _page("ManagerPresence", f"<h1>⏱️</h1><p>{_t(lang, 'err_lien')}</p>", "#C62828"), 404
        if request.args.get("error") or not request.args.get("code"):
            return _page("ManagerPresence", f"<h1>❌</h1><p>{_t(lang, 'err_refus')}</p>", "#C62828"), 400
        try:
            tok = http_requests.post("https://oauth2.googleapis.com/token", data={
                "code": request.args["code"],
                "client_id": GMAIL_CLIENT_ID,
                "client_secret": GMAIL_CLIENT_SECRET,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            }, timeout=20).json()
            if "error" in tok:
                raise RuntimeError(tok.get("error_description") or tok["error"])
            if SCOPE_ENVOI not in (tok.get("scope") or "").split():
                # Consentement granulaire : la case gmail.send a été décochée
                if tok.get("refresh_token"):
                    _revoquer(tok["refresh_token"])
                return _page("ManagerPresence", f"<h1>☑️</h1><p>{_t(lang, 'err_case')}</p>", "#E65100"), 400
            if not tok.get("refresh_token"):
                raise RuntimeError("pas de refresh_token")

            from google.oauth2 import id_token as google_id_token
            from google.auth.transport import requests as google_requests
            infos = google_id_token.verify_oauth2_token(tok["id_token"], google_requests.Request(), GMAIL_CLIENT_ID)
            email = infos.get("email", "")
            if not email or not infos.get("email_verified"):
                raise RuntimeError("email Google non vérifié")

            project_id = lien["project_id"]
            ancienne = autorisation(project_id)
            if ancienne and not hmac.compare_digest(ancienne.get("secret_hash", ""), lien["secret_hash"]):
                # Une autre installation a pris la main entre-temps : on n'écrase pas
                _revoquer(tok["refresh_token"])
                raise RuntimeError("secret différent")
            db.collection("mail_autorisations").document(project_id).set({
                "project_id": project_id,
                "secret_hash": lien["secret_hash"],
                "email": email,
                "club_name": lien.get("club_name", ""),
                "refresh_chiffre": _chiffrer(tok["refresh_token"]),
                "autorise_le": int(time.time()),
                "jour": "", "envoyes": 0,
            })
            db.collection("mail_liens").document(lien_token).update({"utilise": True})
            with verrou:
                jetons_acces.pop(project_id, None)
            if ancienne:
                try:
                    _revoquer(_dechiffrer(ancienne["refresh_chiffre"]))
                except Exception:
                    pass
            print(f"[MAIL] ✅ {project_id} autorisé ({email})")
        except Exception as e:
            print(f"[MAIL] ❌ Callback: {e}")
            return _page("ManagerPresence", f"<h1>⚠️</h1><p>{_t(lang, 'err_technique')}</p>", "#C62828"), 500

        corps = (f"<h1>✅ {_t(lang, 'ok_titre')}</h1>"
                 f"<p>{_t(lang, 'ok_corps').format(club=html.escape(lien.get('club_name', '')), email=html.escape(email))}</p>"
                 f"<p>{_t(lang, 'ok_fermer')}</p>")
        return _page(_t(lang, "titre"), corps, "#2E7D32")

    # ---------------------------------------------------------- Admin

    def est_admin():
        jeton = request.headers.get("Authorization", "").replace("Bearer ", "")
        return bool(admin_token) and hmac.compare_digest(jeton, admin_token)

    @bp.route("/admin/mail", methods=["GET"])
    def admin_mail():
        if not est_admin():
            return jsonify({"erreur": "non autorisé"}), 401
        liste = []
        for doc in db.collection("mail_autorisations").stream():
            a = doc.to_dict()
            liste.append({k: a.get(k) for k in ("project_id", "email", "club_name", "autorise_le", "jour", "envoyes", "dernier_envoi")})
        return jsonify(liste)

    @bp.route("/admin/mail/<project_id>", methods=["DELETE"])
    def admin_mail_supprimer(project_id):
        """Débloque une structure dont le secret est perdu ou a été pris par quelqu'un d'autre."""
        if not est_admin():
            return jsonify({"erreur": "non autorisé"}), 401
        auto = autorisation(project_id)
        if auto:
            try:
                _revoquer(_dechiffrer(auto["refresh_chiffre"]))
            except Exception:
                pass
            db.collection("mail_autorisations").document(project_id).delete()
        return jsonify({"ok": True, "existait": bool(auto)})

    return bp
