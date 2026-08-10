# Separate reusable Music Memory from the planning-specific Music Profile

The Analyser's Material Analyst owns edit-independent analysis of a complete
source track. It extracts tempo, beats, accents, energy, and sections without a
requested output duration and stores the result as reusable Music Memory in the
music Material's analysis directory.

Planner accepts that completed Music Memory rather than analysing the source
track again. The Arrangement Architect projects it onto the requested output
duration as a Music Profile, including duration-dependent truncation or looping.
This boundary lets repeated CLI and service calls share one complete-track
analysis while producing the profile needed by each edit.
