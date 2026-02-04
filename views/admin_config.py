from datetime import datetime, timezone, timedelta

import jwt
import requests
from flask import Blueprint, request, render_template
from wtforms import (
    HiddenField,
    StringField,
)

from CTFd.forms import BaseForm
from CTFd.forms.fields import SubmitField
from CTFd.models import db, Challenges
from CTFd.utils.decorators import (
    admins_only,
)


class ZyncConfig(db.Model):
    """
    Zync Config Model. This model stores the config for Galvanize Instancer API connections.
    """

    id = db.Column(db.Integer, primary_key=True)
    deployer_url = db.Column("deployer_url", db.String(255))
    jwt_secret = db.Column("deployer_secret", db.String(255))


class ZyncConfigForm(BaseForm):
    id = HiddenField()
    deployer_url = StringField(
        "Instancer URL", description="The full URL to the Galvanize Instancer"
    )
    jwt_secret = StringField(
        "JWT Secret",
        description="The secret used to sign JWT tokens for the Instancer",
    )
    submit = SubmitField("Submit")


def define_zync_admin(app):
    admin_zync_config = Blueprint(
        "admin_zync_config",
        __name__,
        template_folder="templates",
        static_folder="assets",
    )

    @admin_zync_config.route("/admin/zync_config", methods=["GET", "POST"])
    @admins_only
    def zync_config():
        config = ZyncConfig.query.filter_by(id=1).first()
        if not config:
            config = ZyncConfig()

        form = ZyncConfigForm(request.form, config)

        if request.method == "POST" and form.validate():
            form.populate_obj(config)

            # Validate against galvanize that we can communicate

            token_payload = {
                "exp": datetime.now(tz=timezone.utc) + timedelta(minutes=180),
                "iat": datetime.now(tz=timezone.utc),
                "user_id": str(0),
                "team_id": str(0),
                "role": "admin",
                "challenge_name": "config_check",
                "category": "config_check",
            }

            token = jwt.encode(
                token_payload, config.jwt_secret, algorithm="HS256"
            )
            try:
                response = requests.post(config.deployer_url + "/admin/config_check", headers={
                    "Authorization": f"Bearer {token}"
                }, timeout=5)

                match response.status_code:
                    case 200:
                        pass
                    case 401:
                        return render_template("zync_config.html", form=form, errors=["Invalid Instancer Secret"])
                    case 403:
                        return render_template("zync_config.html", form=form, errors=["Token role is incorrect, what have we done?"])

            except Exception as e:
                return render_template("zync_config.html", form=form, errors=["Invalid Instancer URL or Secret"])



            db.session.add(config)
            db.session.commit()

            return render_template("zync_config.html", form=form, successes=["Instancer configuration saved"])

        return render_template("zync_config.html", form=form)

    app.register_blueprint(admin_zync_config)
