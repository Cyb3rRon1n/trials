from trials.decide import UserView, decide, user_verdict

N = 3


def test_explicit_vote_wins_over_viewing():
    assert user_verdict(UserView(likes=False, watched=3, finished=3), N) == "dislike"
    assert user_verdict(UserView(likes=True, watched=0, finished=0), N) == "like"


def test_viewing_fallback():
    assert user_verdict(UserView(None, 3, 3), N) == "like"      # finished all trial eps
    assert user_verdict(UserView(None, 2, 1), N) == "dislike"   # stopped early
    assert user_verdict(UserView(None, 1, 0), N) == "dislike"   # started ep 1, quit


def test_never_watched_not_counted():
    assert user_verdict(UserView(None, 0, 0), N) is None


def test_majority_of_engaged():
    views = [UserView(True, 0, 0), UserView(None, 3, 3), UserView(False, 1, 0), UserView(None, 0, 0)]
    assert decide(views, N) == ("keep", 2, 1)


def test_tie_keeps():
    assert decide([UserView(True, 0, 0), UserView(False, 0, 0)], N) == ("keep", 1, 1)


def test_more_dislikes_rejects():
    assert decide([UserView(False, 0, 0), UserView(None, 1, 0), UserView(True, 0, 0)], N) == ("reject", 1, 2)


def test_nobody_engaged_rejects():
    assert decide([UserView(None, 0, 0)] * 5, N) == ("reject", 0, 0)
    assert decide([], N) == ("reject", 0, 0)


def test_single_engaged_like_keeps():
    assert decide([UserView(None, 3, 3)] + [UserView(None, 0, 0)] * 8, N) == ("keep", 1, 0)
