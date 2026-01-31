import json
from datetime import datetime, timedelta, timezone

import jwt
from flask import abort
from flask_restx import Namespace, Resource
from pydantic import BaseModel

from CTFd.api.v1.helpers.request import validate_args
from CTFd.models import Challenges
from CTFd.plugins.zync.views.admin_config import ZyncConfig
from CTFd.utils.decorators import authed_only
from CTFd.utils.user import get_current_team, get_current_user


def getChallenge(challenge_id: int):
    return Challenges.query.filter_by(id=challenge_id).first()


def fail(error_code: int, message: str):
    abort(error_code, json.dumps({"message": message}))


deploy_namespace = Namespace(
    "deploy", description="Endpoints for Galvanize Instancer integration"
)


class DeploymentCreate(BaseModel):
    challenge_id: int


@deploy_namespace.route("/token", methods=["POST"])
class DeploymentTokenAPI(Resource):
    @authed_only
    @validate_args(DeploymentCreate, location="json")
    def post(self, args: dict):
        config = ZyncConfig.query.filter_by(id=1).first()
        if not config or not config.jwt_secret:
            fail(500, "Instancer not configured")

        user = get_current_user()
        team = get_current_team()
        challenge = getChallenge(args["challenge_id"])
        if not challenge:
            fail(400, "Invalid challenge ID")

        category = challenge.category

        token_payload = {
            "exp": datetime.now(tz=timezone.utc) + timedelta(minutes=180),
            "iat": datetime.now(tz=timezone.utc),
            "user_id": str(user.id),
            "team_id": str(team.id) if team else None,
            "role": "user",
            "challenge_name": challenge.name,
            "category": category,
        }

        token = jwt.encode(
            token_payload, config.jwt_secret, algorithm="HS256"
        )
        return {"token": token}


@deploy_namespace.route("/url", methods=["GET"])
class DeploymentURLAPI(Resource):
    @authed_only
    def get(self):
        config = ZyncConfig.query.filter_by(id=1).first()
        if not config:
            fail(500, "Instancer not configured")
        return {"deployer_url": config.deployer_url}
