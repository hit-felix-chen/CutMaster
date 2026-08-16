# Separate reusable Music Memory from the Planners-specific Music Profile

**Status: Implemented.**

The Analyser's Material Analyst owns edit-independent analysis of a complete
source track. It extracts tempo, beats, accents, energy, and sections without a
requested output duration and stores the result as reusable Music Memory in the
music Material's analysis directory.

The Planners stage accepts that completed Music Memory rather than analysing
the source track again. The Arrangement Architect projects it onto the
requested output duration as a Music Profile, including duration-dependent
truncation or looping. This boundary lets repeated CLI and service calls share
one complete-track analysis while producing the profile needed by each edit.

At runtime the Application supplies that Memory through an analysed Material
handle bound to the exact Material ID, fingerprint, Memory version, and leased
paths. Planners receives neither a raw audio path nor a Material Name selector;
those concerns terminate at the Application boundary.
