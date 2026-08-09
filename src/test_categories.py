from src.intelligence import classify


def test_sports_story():
    item = {
        "primary_source": False,
        "tier": 2,
    }

    result = classify(
        "South Korea football association apologises over allegations",
        "The football association said it was investigating the allegations.",
        "world",
        item,
    )

    assert result["category"] == "sports", result


def test_earthquake_story():
    item = {
        "primary_source": True,
        "tier": 1,
    }

    result = classify(
        "Major earthquake strikes region",
        "Authorities are assessing damage after the earthquake.",
        "disaster",
        item,
    )

    assert result["category"] == "disaster", result


def test_nasa_story():
    item = {
        "primary_source": True,
        "tier": 1,
    }

    result = classify(
        "NASA announces new space mission",
        "Scientists will study the Moon during the mission.",
        "space",
        item,
    )

    assert result["category"] == "space", result


def test_finance_story():
    item = {
        "primary_source": True,
        "tier": 1,
    }

    result = classify(
        "Central bank announces new interest rate",
        "The central bank changed its interest rate policy.",
        "finance",
        item,
    )

    assert result["category"] == "finance", result


def test_politics_story():
    item = {
        "primary_source": False,
        "tier": 2,
    }

    result = classify(
        "Prime minister announces election plans",
        "The government announced new election plans.",
        "politics",
        item,
    )

    assert result["category"] == "politics", result
    
def test_ebola_story_is_health_not_region():
    item = {
        "primary_source": False,
        "tier": 2,
    }

    result = classify(
        "More than 300 children have died from Ebola since start of DRC outbreak",
        "More than 300 children have died from Ebola since the outbreak began in the Democratic Republic of Congo.",
        "africa",
        item,
    )

    assert result["category"] == "health", result
def test_drone_wildfire_story():
    item = {
        "primary_source": False,
        "tier": 2,
    }

    result = classify(
        "How Turkey uses drones to tackle fires",
        "Emergency services are using drones to detect wildfires at an earlier stage.",
        "world",
        item,
    )

    assert result["category"] == "technology", result


def test_crocodile_climate_story():
    item = {
        "primary_source": False,
        "tier": 2,
    }

    result = classify(
        "Crocodile attacks increase in Kenya",
        "Extreme rainfall linked to climate change is causing dangerous wildlife encounters.",
        "world",
        item,
    )

    assert result["category"] == "environment", result
def test_government_court_story():
    item = {
        "primary_source": False,
        "tier": 2,
    }

    result = classify(
        "Trump vows appeal over ballroom halt",
        "The US President said he would appeal after a federal appeals court halted the White House ballroom project.",
        "world",
        item,
    )

    assert result["category"] == "politics", result
