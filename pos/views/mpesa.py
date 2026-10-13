"""Daraja STK callback endpoint (public, no login, no CSRF)."""
import secrets

from flask import Blueprint, current_app, jsonify, request

from .. import sales as svc
from ..db import audit, get_db
from ..mpesa import RESULT_STATUS, MpesaError, parse_callback
from ..settings import get_all

bp = Blueprint("mpesa", __name__, url_prefix="/mpesa")


@bp.post("/callback/<token>")
def callback(token):
    db = get_db()
    # The secret token in the URL means random internet traffic can't post fake payments.
    if not secrets.compare_digest(token, get_all(db)["mpesa_callback_token"]):
        return jsonify(ResultCode=1, ResultDesc="Rejected"), 403
    body = request.get_json(force=True, silent=True)
    try:
        cb = parse_callback(body)
    except MpesaError:
        return jsonify(ResultCode=1, ResultDesc="Bad payload"), 400
    known = db.execute("SELECT 1 FROM payments WHERE checkout_request_id = ?", (cb["checkout_request_id"],)).fetchone()
    if not known:
        audit(db, None, "mpesa_unknown_callback", str(body)[:1000])
        return jsonify(ResultCode=0, ResultDesc="Accepted")
    if cb["result_code"] == "0":
        svc.complete_mpesa(db, cb["checkout_request_id"], cb["receipt"], cb["amount"], cb["phone"], body)
    else:
        svc.fail_mpesa(db, cb["checkout_request_id"], cb["result_desc"], body,
                       status="cancelled" if RESULT_STATUS.get(cb["result_code"]) == "cancelled" else "failed")
    current_app.logger.info("M-Pesa callback %s -> %s", cb["checkout_request_id"], cb["result_code"])
    return jsonify(ResultCode=0, ResultDesc="Accepted")
