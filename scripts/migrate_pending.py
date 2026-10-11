"""Kept so older instructions still work: the optional modules' migrations are ordinary ones now.

    python -m scripts.migrate_pending     # the same as: python -m scripts.migrate
"""

from scripts.migrate import main

if __name__ == "__main__":
    main()
