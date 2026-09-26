from dataclasses import dataclass


@dataclass(frozen=True)
class UserView:
    likes: bool | None
    watched: int
    finished: int
    favorite: bool = False   # ♥ = a like worth two votes (beats that user's own 👎)


def user_verdict(v, trial_episodes):
    if v.favorite:
        return "fav"
    if v.likes is not None:
        return "like" if v.likes else "dislike"
    if v.watched == 0:
        return None
    return "like" if v.finished >= trial_episodes else "dislike"


def decide(views, trial_episodes):
    verdicts = [user_verdict(v, trial_episodes) for v in views]
    likes, dislikes = verdicts.count("like") + 2 * verdicts.count("fav"), verdicts.count("dislike")
    if likes + dislikes == 0:
        return "reject", 0, 0
    return ("keep" if likes >= dislikes else "reject"), likes, dislikes
