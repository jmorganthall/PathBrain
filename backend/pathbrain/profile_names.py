"""Memorable **call signs** for settings profiles — "Speedy Sloth", not "q1514 t5ms".

A profile's identity is a 12-hex fingerprint, and its human label is a settings summary
(``wan: 900Mbit q1514 t5ms``). Both are precise and both are unreadable in a ranking: a
field of 150 profiles differing in one number is a wall of near-identical strings, and a
duel between two of them reads as noise. A name you can *say* — the thing sports leagues,
hurricanes and datacentres all converge on — makes a standings table scannable and a bout
tape narratable.

**Deterministically derived, not AI-generated**, on purpose:

* *Stable* — the name is a pure function of the fingerprint plus what's already taken, so
  a profile is never renamed behind the user's back and old duel tapes still read true.
* *Offline and instant* — no API key, no per-profile call, no failure mode where naming a
  profile costs money or hangs a page load.
* *Unique by construction* — the assignment is persisted (``ProfileName``) and probes past
  collisions, so two profiles can never share a call sign. A model sampling names would
  need a uniqueness pass anyway, and would drift between runs.

Names are alliterative *by preference* (the hash picks an adjective, then prefers a noun
sharing its initial) and fall back to any noun when that pool is exhausted — memorable
where possible, unique always.
"""
from __future__ import annotations

import hashlib

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .database import session_scope
from .logging_config import get_logger
from .models import ProfileName

log = get_logger("profile_names")

# The one profile that gets a fixed name: SQM off isn't a tuning choice, it's the control
# group, and calling it "Zippy Zebra" would bury the one comparison that matters.
SQM_OFF_NAME = "No Shaper"

# ~500 of each: a quarter-million pairings, so the name a profile gets is effectively its
# own and the collision probe below almost never has to fire. Grouped by initial (the
# alliteration preference reads them that way) and deliberately plain — evocative,
# pronounceable, nothing that reads as a slur, a brand, or a person.
ADJECTIVES = tuple(
    """
    Agile Airy Alert Alpine Amber Ambling Ample Ancient Amiable Arctic
    Ardent Artful Ashen Astral Auburn August Aurora Autumn Avid Azure
    Balmy Bantam Beaming Blithe Bold Bonny Boreal Bouncy Boundless Brave
    Brawny Breezy Bright Brilliant Brisk Bronze Bubbly Buoyant Burly Bustling
    Calm Candid Canny Canyon Capital Carefree Cascading Cedar Celestial Cheerful
    Cheery Chipper Chromatic Civil Classic Clever Cobalt Comet Coral Cosmic
    Courtly Cozy Crafty Creamy Crescent Crimson Crisp Crystal Cunning Curious
    Dainty Damask Dandy Dapper Daring Dashing Dauntless Dawn Dazzling Deft
    Delicate Dewy Diligent Dimpled Direct Dizzy Doughty Downy Dreamy Driven
    Drifting Dusky Dusty Dutiful Dynamic Eager Earnest Earthy Easy Ebony
    Eclectic Electric Elegant Elder Elemental Emerald Ember Eminent Endless Enchanted
    Epic Equable Eternal Ethereal Even Exact Exuberant Fabled Fair Faithful
    Fanciful Far Fearless Feathered Feisty Fervent Festive Fiery Fleet Flinty
    Floral Fluent Flying Foamy Focused Fond Forest Formal Fortunate Frank
    Free Fresh Frosty Fluid Gallant Game Garnet Gauzy Gentle Genuine
    Giddy Gilded Gingham Glacial Glad Glassy Gleaming Gliding Glimmering Glossy
    Golden Graceful Grand Granite Grassy Gritty Guiding Gusty Halcyon Hale
    Hallowed Handy Happy Harbor Hardy Harmonic Hasty Haughty Hazel Hazy
    Hearty Heather Heavenly Hidden Highland Hollow Homely Honest Hopeful Humble
    Hushed Husky Icy Ideal Idle Immense Imperial Indigo Ingenious Inky
    Intent Intrepid Inventive Iron Island Ivory Ivied Jade Jagged Jaunty
    Jazzy Jeweled Jolly Jovial Joyful Jubilant Judicious Juniper Just Keen
    Kindly Kinetic Kingly Knightly Knotty Kindred Lacquered Lambent Languid Lantern
    Larkspur Lasting Laurel Lavish Leafy Lean Level Light Lilac Limber
    Lithe Lively Lofty Lonesome Loyal Lucid Lucky Lumbering Luminous Lunar
    Lush Magnetic Majestic Marble Maritime Marvelous Meadow Mellow Merry Meteoric
    Midnight Mighty Mild Mindful Minty Mirthful Misty Modest Molten Moonlit
    Mossy Mountain Mystic Native Natty Neat Nether Nifty Nimble Noble
    Nocturnal Nomadic Northern Notable Novel Nutmeg Oaken Obsidian Ocean Onyx
    Opal Opaline Orbital Ornate Outbound Outland Pacific Painted Pale Palm
    Paper Patient Peaceful Pearly Peppery Peppy Perennial Perky Petite Pewter
    Pine Pioneer Piquant Placid Plain Playful Plucky Plum Polar Polished
    Poplar Prairie Prancing Precise Prime Pristine Prompt Proper Prudent Pure
    Purple Quaint Quantum Quartz Quick Quiet Quilted Quirky Quixotic Radiant
    Rambling Rapid Rare Reckless Regal Reliant Restless Rich Rippling Roaming
    Robust Rolling Rooted Rosy Rowdy Royal Ruby Rugged Rustic Sable
    Saffron Sage Salty Sandy Sapphire Satin Savvy Scarlet Scenic Seaborne
    Sedate Serene Shaded Sharp Sheer Shining Shiny Silent Silken Silver
    Simple Sincere Singing Skyward Sleek Slender Smoky Snowy Solar Solemn
    Sonic Soaring Southern Spangled Sparkling Speedy Spirited Splendid Spry Stalwart
    Starlit Steadfast Steady Stellar Stony Stormy Stout Striped Sturdy Sublime
    Subtle Sunlit Sunny Supple Svelte Swift Sylvan Tacit Tactful Tall
    Tamed Tangy Tawny Teal Tempered Tender Terrific Thoughtful Thriving Thundering
    Tidal Tidy Timber Timely Tireless Topaz Towering Tranquil Trim Triumphant
    True Trusty Tumbling Twilight Twinkling Ultra Umber Unbound Unerring Unhurried
    Untamed Upbeat Upland Upright Urban Urbane Utmost Vagrant Valiant Valorous
    Vast Velvet Verdant Vernal Vesper Vibrant Vigilant Vigorous Violet Vivid
    Vocal Voyaging Wandering Wanton Warm Watchful Waverly Wayward Wealthy Western
    Whimsical Whirling Whispering Wild Willing Willow Wily Windswept Windy Winsome
    Winter Wise Wistful Witty Wondrous Woodland Woven Xenial Xeric Yearning
    Yellow Yielding Yonder Youthful Yuletide Zealous Zenith Zesty Zippy Zircon
""".split()
)

NOUNS = tuple(
    """
    Acorn Albatross Alcove Alder Alloy Alpaca Anchor Anemone Angler Antelope
    Anvil Apex Arbor Archer Armada Arrow Ascent Aspen Atlas Aurora
    Avalanche Aviator Axle Badger Ballad Balloon Bandit Banner Barnacle Barracuda
    Basin Bastion Bayou Beacon Bear Beaver Bellows Bison Bittern Blossom
    Bluff Bobcat Boulder Bramble Breaker Brigantine Brindle Brook Buffalo Bulwark
    Bumblebee Bunting Burrow Buzzard Cactus Cairn Caldera Camel Canyon Capybara
    Caravan Cardinal Caribou Carousel Cascade Castle Catamaran Cavern Cedar Chalice
    Chameleon Chariot Cheetah Chestnut Chinook Chipmunk Cinder Cirrus Clipper Cloudlet
    Clover Cobra Comet Compass Condor Coracle Cormorant Corsair Cottage Cougar
    Coyote Crane Crater Crescent Cricket Crossbow Crow Cygnet Cypress Dahlia
    Dalmatian Dandelion Dart Delta Dingo Dolphin Dormouse Dove Dragon Dragonfly
    Drake Drifter Drummer Dune Dynamo Eagle Echo Eddy Eel Egret
    Elder Elk Ember Emissary Emu Engine Envoy Escarpment Estuary Falcon
    Fathom Fawn Feather Fennec Ferret Fern Ferry Fiddle Finch Firefly
    Fjord Flamingo Flint Flotilla Flume Foothill Forge Fossa Foundry Fountain
    Foxglove Foxhound Frigate Fulmar Furrow Gable Galleon Gannet Garland Gazelle
    Gecko Geode Geyser Gibbon Glacier Glade Gondola Goshawk Granite Grebe
    Greyhound Griffin Grotto Grove Guardian Gull Gully Gyrfalcon Halibut Hamlet
    Hammock Harbor Harrier Harvest Hawk Hazel Headland Hearth Heather Hedgehog
    Helm Heron Highland Hollow Homestead Hornbill Hornet Horizon Hummingbird Hurricane
    Husky Hyacinth Ibex Ibis Iceberg Icicle Impala Inlet Iris Ironwood
    Islet Ivy Jackal Jackdaw Jaguar Jasmine Javelin Jay Jetty Jonquil
    Juniper Junco Jungle Kangaroo Kayak Kelp Kestrel Kettle Keystone Kingfisher
    Kinglet Kite Kitten Koala Kraken Krill Lagoon Lancer Lantern Lapwing
    Lark Lattice Lavender Ledge Lemming Lemur Leopard Levee Lighthouse Lilac
    Linden Lion Llama Lobster Locket Longboat Lookout Lotus Lupine Lynx
    Lyre Macaw Magnolia Magpie Mallard Mammoth Mandolin Mangrove Manta Maple
    Marlin Marmot Marsh Marten Mast Meadow Meerkat Meridian Mesa Meteor
    Minnow Mockingbird Mongoose Monsoon Moorland Moraine Moth Mustang Myrtle Narwhal
    Nautilus Nebula Nectar Needle Nettle Newt Nightjar Nimbus Nomad Nutcracker
    Nuthatch Oak Oasis Obelisk Ocelot Octopus Olive Onager Opal Orbit
    Orca Orchard Orchid Oriole Osprey Otter Outcrop Outrider Owl Oyster
    Paddock Palomino Panther Papyrus Parapet Parsnip Partridge Passage Peacock Pelican
    Pendant Penguin Peregrine Petrel Pheasant Pigeon Pike Pilgrim Pillar Pinnacle
    Pioneer Piper Plateau Plover Plume Polaris Pollen Pony Poplar Porpoise
    Prairie Prism Puffin Puma Pyre Quail Quarry Quartz Quasar Quay
    Quiver Quokka Quoll Rabbit Raccoon Rambler Rampart Ranger Rapids Raven
    Ravine Redwood Reef Reindeer Ridge Rill Ripple Roadrunner Robin Rockfish
    Rook Rooster Rover Rowan Rudder Runner Rye Sable Saddle Saffron
    Sage Sailfish Salamander Salmon Saltmarsh Sandpiper Sapling Sapphire Sardine Savanna
    Schooner Scout Seahorse Sentinel Sequoia Serpent Shale Shepherd Sherpa Shipwright
    Shoal Shrike Sierra Signal Silo Siskin Skiff Skylark Sloop Sloth
    Snipe Sparrow Spinnaker Spire Springbok Spruce Squall Stallion Starling Steppe
    Stingray Stoat Stork Strand Summit Sunfish Surf Swallow Swan Sycamore
    Talon Tamarack Tamarin Tanager Tapir Teal Tempest Terrace Tern Thicket
    Thimble Thistle Thrush Tidepool Tiger Timberline Toucan Tower Trailhead Trawler
    Trellis Trillium Trout Tulip Tundra Turnstone Turret Turtle Tusker Umbra
    Umbrella Unicorn Upland Urchin Ursa Vale Valley Vanguard Vaquero Vault
    Venture Verdure Vessel Viceroy Vine Viper Vireo Vista Voyager Vulture
    Wagtail Walrus Wanderer Warbler Warren Waterfall Wavelet Waypoint Weasel Whale
    Wharf Whippet Whirlwind Whistler Widgeon Wildcat Willow Windmill Wolverine Wombat
    Woodpecker Wren Xebec Xerus Yak Yarrow Yearling Yellowtail Yeoman Yew
    Yucca Zebra Zenith Zephyr Zeppelin Zinnia
""".split()
)

_NOUNS_BY_INITIAL: dict[str, tuple[str, ...]] = {}
for _n in NOUNS:
    _NOUNS_BY_INITIAL.setdefault(_n[0], ())
    _NOUNS_BY_INITIAL[_n[0]] += (_n,)

# Odd steps so repeated probing walks the whole list instead of cycling on a short orbit.
_ADJ_STEP = 37
_NOUN_STEP = 53
_MAX_ATTEMPTS = 512


def _seed(fingerprint: str) -> int:
    """A stable 64-bit seed for a fingerprint (blake2b — not Python's salted hash())."""
    return int.from_bytes(
        hashlib.blake2b(fingerprint.encode("utf-8"), digest_size=8).digest(), "big"
    )


def candidates(fingerprint: str):
    """The deterministic stream of names this fingerprint would like, best first.

    Attempt 0 is its "natural" name; later attempts walk adjective and noun together so a
    contested name degrades to a different pairing rather than a numbered suffix. The
    final fallback appends four hex characters of the fingerprint, which cannot collide.
    """
    seed = _seed(fingerprint)
    for attempt in range(_MAX_ATTEMPTS):
        adjective = ADJECTIVES[(seed + attempt * _ADJ_STEP) % len(ADJECTIVES)]
        # Alliteration by preference: prefer a noun sharing the adjective's initial, and
        # fall back to the full list when that letter has nothing left to offer.
        pool = _NOUNS_BY_INITIAL.get(adjective[0]) or NOUNS
        if attempt >= len(pool) * 2:  # that letter is crowded — open it up
            pool = NOUNS
        noun = pool[((seed >> 17) + attempt * _NOUN_STEP) % len(pool)]
        yield f"{adjective} {noun}"
    yield f"{ADJECTIVES[seed % len(ADJECTIVES)]} {NOUNS[(seed >> 17) % len(NOUNS)]} {fingerprint[:4]}"


def _is_sqm_off(fingerprint: str) -> bool:
    from .settings_profile import SQM_OFF_FINGERPRINT

    return fingerprint == SQM_OFF_FINGERPRINT


def name_for(session, fingerprint: str) -> str:
    """This profile's call sign, assigning one on first sight.

    ``session`` is used only to *read*. Assignment deliberately opens its own transaction
    (``session_scope``) instead of writing through the caller's: request sessions come
    from the read-only ``get_session`` dependency, which closes without committing — a
    name written there would evaporate, and the next request would re-derive it against a
    different set of taken names. The uniqueness guarantee only holds if the assignment
    is committed the moment it's made.
    """
    if not fingerprint:
        return "—"
    row = session.get(ProfileName, fingerprint) if session is not None else None
    if row is not None:
        return row.name
    return assign(fingerprint)


def assign(fingerprint: str) -> str:
    """Claim (and commit) this fingerprint's call sign, returning the existing one if any."""
    stream = iter([SQM_OFF_NAME]) if _is_sqm_off(fingerprint) else candidates(fingerprint)
    with session_scope() as session:
        row = session.get(ProfileName, fingerprint)
        if row is not None:
            return row.name
        return _claim(session, fingerprint, stream)


def _claim(session, fingerprint: str, stream) -> str:
    """Persist the first candidate name not already taken by another profile."""
    taken = {n for (n,) in session.execute(select(ProfileName.name))}
    for candidate in stream:
        if candidate in taken:
            continue
        try:
            with session.begin_nested():  # savepoint: a lost race must not poison the tx
                session.add(ProfileName(fingerprint=fingerprint, name=candidate))
            return candidate
        except IntegrityError:
            existing = session.get(ProfileName, fingerprint)
            if existing is not None:  # someone named this very profile first — take theirs
                return existing.name
            taken.add(candidate)  # someone took the name; try the next candidate
    # Unreachable: the stream ends with a fingerprint-suffixed name that cannot collide.
    return fingerprint[:8]


def names_for(session, fingerprints) -> dict[str, str]:
    """Call signs for many fingerprints in one pass (one query, then assign the misses).

    Used by every list view, so a 150-profile standings table costs one query rather than
    150 — naming must never become a reason a page got slower.
    """
    wanted = [fp for fp in dict.fromkeys(fingerprints) if fp]
    if not wanted:
        return {}
    known = {
        r.fingerprint: r.name
        for r in session.scalars(select(ProfileName).where(ProfileName.fingerprint.in_(wanted)))
    }
    for fp in wanted:
        if fp not in known:
            known[fp] = assign(fp)
    return known


def rename(fingerprint: str, name: str) -> str:
    """Set a profile's call sign by hand. Raises ``ValueError`` if empty or taken."""
    name = " ".join((name or "").split())[:60]
    if not name:
        raise ValueError("a name cannot be empty")
    with session_scope() as session:
        clash = session.scalars(
            select(ProfileName).where(
                ProfileName.name == name, ProfileName.fingerprint != fingerprint
            )
        ).first()
        if clash is not None:
            raise ValueError(f"{name!r} is already taken by another profile")
        row = session.get(ProfileName, fingerprint)
        if row is None:
            session.add(ProfileName(fingerprint=fingerprint, name=name))
        else:
            row.name = name
    log.info("Profile %s renamed to %r", fingerprint, name)
    return name


__all__ = [
    "ADJECTIVES",
    "NOUNS",
    "SQM_OFF_NAME",
    "assign",
    "candidates",
    "name_for",
    "names_for",
    "rename",
]
