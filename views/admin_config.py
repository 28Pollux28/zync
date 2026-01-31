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

            db.session.add(config)
            db.session.commit()

        return render_template("zync_config.html", form=form)

    app.register_blueprint(admin_zync_config)
