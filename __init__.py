import os

from CTFd.api import CTFd_API_v1
from CTFd.plugins import register_plugin_assets_directory
from CTFd.plugins.challenges import CHALLENGE_CLASSES

from .views.admin_dashboard import define_zync_admin_dashboard
from .api.deploy import deploy_namespace
from .models.challenge_type import ZyncChallengeType
from .views.admin_config import define_zync_admin, ZyncConfig

__version__ = "0.5.0"


def load(app):
    app.db.create_all()
    
    # Load configuration from environment variables at startup
    deployer_url = os.getenv("ZYNC_DEPLOYER_URL")
    jwt_secret = os.getenv("ZYNC_JWT_SECRET")
    
    if deployer_url or jwt_secret:
        with app.app_context():
            config = ZyncConfig.query.filter_by(id=1).first()
            if not config:
                config = ZyncConfig(id=1)
            
            # Update config from env vars if they are set
            if deployer_url:
                config.deployer_url = deployer_url
            if jwt_secret:
                config.jwt_secret = jwt_secret
            
            app.db.session.add(config)
            app.db.session.commit()
    
    CHALLENGE_CLASSES["zync"] = ZyncChallengeType
    register_plugin_assets_directory(
        app, base_path="/plugins/zync/assets"
    )
    define_zync_admin(app)
    define_zync_admin_dashboard(app)
    CTFd_API_v1.add_namespace(deploy_namespace, "/deploy")
