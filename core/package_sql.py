"""Shared immutable SQL expressions for the package index and read queries.

These expressions define schema v18: changing them requires a new migration,
otherwise existing databases may retain a different indexed expression.
"""

_UNIT = "substr(coalesce(teryt_dzialki, ''), 1, 6)"
# NULL-safe equivalent of models.location_mismatch. CASE stops early for absent
# geometry/municipality; SQL AND expressions in an aggregate need not do so.
PARCEL_SQL = f"""CASE
    WHEN precyzja_geo IS NOT 'dzialka' THEN 0
    WHEN typeof(lat) NOT IN ('real', 'integer') OR lat NOT BETWEEN -90 AND 90 THEN 0
    WHEN typeof(lon) NOT IN ('real', 'integer') OR lon NOT BETWEEN -180 AND 180 THEN 0
    WHEN coalesce(gmina_teryt, '') = '' THEN 1
    WHEN length({_UNIT}) <> 6 OR {_UNIT} GLOB '*[^0-9]*' THEN 1
    ELSE substr(gmina_teryt, 1, 6) = {_UNIT} END"""
SCOPE_SQL = """is_noise = 0 AND status IN ('wniosek', 'decyzja', 'zgloszenie', 'brak_sprzeciwu')
    AND powiat_teryt GLOB '[0-9][0-9][0-9][0-9]'"""

# Only eligible cases enter this covering expression index. All fields used by
# estimates are available without random full-row reads. Existing indexes stay
# in place for other engine queries. SQLite maintains quality on every write.
PACKAGE_INDEX_SQL = f"""CREATE INDEX ix_investments_package ON investments (
    powiat_teryt, kategoria, ({PARCEL_SQL}), lat, lon, data_decyzji, data_wplywu, ostatnio_widziany
) WHERE {SCOPE_SQL};"""
