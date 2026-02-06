from CTFd.api import CTFd_API_v1
from CTFd.plugins import register_plugin_assets_directory
from CTFd.plugins.challenges import CHALLENGE_CLASSES

from .views.admin_dashboard import define_zync_admin_dashboard
from .api.deploy import deploy_namespace
from .models.challenge_type import ZyncChallengeType
from .views.admin_config import define_zync_admin


def load(app):
    app.db.create_all()
    CHALLENGE_CLASSES["zync"] = ZyncChallengeType
    register_plugin_assets_directory(
        app, base_path="/plugins/zync/assets"
    )
    define_zync_admin(app)
    define_zync_admin_dashboard(app)
    CTFd_API_v1.add_namespace(deploy_namespace, "/deploy")
