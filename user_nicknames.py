"""Generate friendly, stable-by-storage default display names for Minto users."""
import secrets

ADJECTIVES = (
    "Cosmic", "Velvet", "Neon", "Misty", "Turbo", "Clever", "Sunny", "Midnight",
    "Pixel", "Lucky", "Frosty", "Golden", "Mellow", "Electric", "Witty", "Jolly",
    "Mystic", "Crispy", "Dapper", "Orbiting",
)
NOUNS = (
    "Panda", "Fox", "Falcon", "Koala", "Raven", "Otter", "Comet", "Badger",
    "Penguin", "Tiger", "Corgi", "Phoenix", "Mango", "Bison", "Sparrow", "Lynx",
    "Dolphin", "Puffin", "Gecko", "Firefly",
)


def generate_random_nickname():
    """Return a short, playful nickname; callers persist it as display_name."""
    return f"{secrets.choice(ADJECTIVES)} {secrets.choice(NOUNS)}"
