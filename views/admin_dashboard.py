from datetime import datetime, timezone, timedelta

import jwt
from flask import Blueprint, render_template, jsonify, request

from CTFd.models import Challenges
from CTFd.utils.decorators import admins_only
from .admin_config import ZyncConfig


def define_zync_admin_dashboard(app):
    admin_zync_dashboard = Blueprint(
        "admin_zync_dashboard",
        __name__,
        template_folder="templates",
        static_folder="assets",
    )

    @admin_zync_dashboard.route("/admin/zync_dashboard", methods=["GET"])
    @admins_only
    def zync_dashboard():
        return render_template("zync_admin_dashboard.html")

    @admin_zync_dashboard.route("/admin/zync_token", methods=["GET"])
    @admins_only
    def get_admin_token():
        """Generate and return an admin JWT token for the current session"""
        config = ZyncConfig.query.filter_by(id=1).first()

        if not config or not config.jwt_secret or not config.deployer_url:
            return jsonify({"error": "Zync not configured"}), 500

        token_payload = {
            "exp": datetime.now(tz=timezone.utc) + timedelta(hours=2),
            "iat": datetime.now(tz=timezone.utc),
            "user_id": str(0),
            "team_id": str(0),
            "role": "admin",
            "challenge_name": "",
            "category": "",
        }

        token = jwt.encode(token_payload, config.jwt_secret, algorithm="HS256")

        return jsonify({
            "token": token,
            "api_url": config.deployer_url
        })

    @admin_zync_dashboard.route("/admin/zync_status_token", methods=["POST"])
    @admins_only
    def get_status_token():
        """Generate a status token for a specific challenge"""
        config = ZyncConfig.query.filter_by(id=1).first()

        if not config or not config.jwt_secret:
            return jsonify({"error": "Zync not configured"}), 500

        data = request.get_json()
        category = data.get("category")
        challenge_name = data.get("challenge_name")

        if not category or not challenge_name:
            return jsonify({"error": "Missing category or challenge_name"}), 400

        # Get challenge ID from CTFd database
        challenge = Challenges.query.filter_by(name=challenge_name, category=category).first()

        if not challenge:
            return jsonify({"error": "Challenge not found"}), 404

        token_payload = {
            "exp": datetime.now(tz=timezone.utc) + timedelta(hours=2),
            "iat": datetime.now(tz=timezone.utc),
            "user_id": str(0),
            "team_id": str(0),
            "role": "admin",
            "challenge_name": challenge_name,
            "category": category,
        }

        token = jwt.encode(token_payload, config.jwt_secret, algorithm="HS256")

        return jsonify({
            "token": token,
            "challenge_id": challenge.id
        })

    app.register_blueprint(admin_zync_dashboard)
