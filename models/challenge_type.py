from flask import Blueprint

from CTFd.models import Challenges, db
from CTFd.plugins.challenges import BaseChallenge


class ZyncChallenge(Challenges):
    __mapper_args__ = {"polymorphic_identity": "zync"}
    id = db.Column(
        db.Integer, db.ForeignKey("challenges.id", ondelete="CASCADE"), primary_key=True
    )

    def __init__(self, *args, **kwargs):
        kwargs["type"] = "zync"
        super().__init__(*args, **kwargs)


class ZyncChallengeType(BaseChallenge):
    id = "zync"
    name = "zync"
    templates = {
        "create": "/plugins/zync/assets/create.html",
        "update": "/plugins/zync/assets/update.html",
        "view": "/plugins/zync/assets/view.html",
    }
    scripts = {
        "create": "/plugins/zync/assets/create.js",
        "update": "/plugins/zync/assets/update.js",
        "view": "/plugins/zync/assets/view.js",
    }
    route = "/plugins/zync/assets"
    blueprint = Blueprint(
        "zync",
        __name__,
        template_folder="templates",
        static_folder="assets",
    )
    challenge_model = ZyncChallenge

    @classmethod
    def delete(cls, challenge):
        """
        This method is used to delete the resources used by a challenge.
        Instance lifecycle is managed by Galvanize Instancer.
        """
        super().delete(challenge)

    @classmethod
    def read(cls, challenge):
        """
        This method is in used to access the data of a challenge in a format processable by the front end.

        :param challenge:
        :return: Challenge object, data dictionary to be returned to the user
        """
        challenge = ZyncChallenge.query.filter_by(id=challenge.id).first()
        data = super().read(challenge)
        return data

    @classmethod
    def solve(cls, user, team, challenge, request):
        """
        This method is used to insert Solves into the database in order to mark a challenge as solved.

        :param team: The Team object from the database
        :param chal: The Challenge object from the database
        :param request: The request the user submitted
        :return:
        """
        return super().solve(user, team, challenge, request)
