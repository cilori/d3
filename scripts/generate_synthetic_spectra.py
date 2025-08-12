import os
import math
import csv
from dataclasses import dataclass
from typing import Dict, List, Tuple


@dataclass
class Spectrum:
    name: str
    wavelengths_um: List[float]
    reflectance: List[float]


def generate_wavelengths(start_um: float = 0.40, end_um: float = 2.50, step_um: float = 0.005) -> List[float]:
    wavelengths = []
    current = start_um
    # Add small epsilon to ensure inclusion of end
    while current <= end_um + 1e-9:
        wavelengths.append(round(current, 6))
        current = round(current + step_um, 6)
    return wavelengths


def linear_baseline(wavelengths: List[float], low_reflectance: float, high_reflectance: float) -> List[float]:
    wl_min = wavelengths[0]
    wl_max = wavelengths[-1]
    span = wl_max - wl_min
    if span <= 0:
        return [low_reflectance for _ in wavelengths]
    values = []
    for wl in wavelengths:
        t = (wl - wl_min) / span
        values.append(low_reflectance + t * (high_reflectance - low_reflectance))
    return values


def gaussian_factor(wavelengths: List[float], center_um: float, width_um: float, depth: float) -> List[float]:
    # Multiplicative absorption factor centered at center_um
    # depth in [0, 1), width is sigma-like (controls spread)
    two_sigma_sq = 2.0 * (width_um ** 2)
    out = []
    for wl in wavelengths:
        exponent = -((wl - center_um) ** 2) / max(two_sigma_sq, 1e-12)
        out.append(1.0 - max(0.0, min(depth, 0.95)) * math.exp(exponent))
    return out


def apply_absorptions(baseline: List[float], wavelengths: List[float], dips: List[Tuple[float, float, float]]) -> List[float]:
    # Apply multiplicative Gaussians to the baseline
    out = baseline[:]
    for center, width, depth in dips:
        factor = gaussian_factor(wavelengths, center, width, depth)
        out = [max(0.0, min(0.98, r * f)) for r, f in zip(out, factor)]
    return out


def clamp(values: List[float], min_val: float = 0.02, max_val: float = 0.98) -> List[float]:
    return [max(min_val, min(max_val, v)) for v in values]


def make_mineral_spectrum(
    name: str,
    wavelengths: List[float],
    low_reflectance: float,
    high_reflectance: float,
    dips: List[Tuple[float, float, float]],
) -> Spectrum:
    base = linear_baseline(wavelengths, low_reflectance, high_reflectance)
    refl = apply_absorptions(base, wavelengths, dips)
    return Spectrum(name=name, wavelengths_um=wavelengths, reflectance=clamp(refl))


def mix_spectra(wavelengths: List[float], components: List[Tuple[Spectrum, float]], name: str) -> Spectrum:
    # Linear mixture by weight fraction
    weight_sum = sum(w for _, w in components)
    if weight_sum <= 0:
        raise ValueError("Sum of component weights must be > 0")
    norm_components = [(spec, w / weight_sum) for spec, w in components]
    mixed = [0.0 for _ in wavelengths]
    for spec, w in norm_components:
        if spec.wavelengths_um != wavelengths:
            raise ValueError(f"Wavelength grids do not match for component {spec.name}")
        for i in range(len(wavelengths)):
            mixed[i] += w * spec.reflectance[i]
    return Spectrum(name=name, wavelengths_um=wavelengths, reflectance=clamp(mixed))


def write_csv(path: str, spectra: List[Spectrum]) -> None:
    if not spectra:
        return
    wavelengths = spectra[0].wavelengths_um
    header = ["Wavelength_um"] + [s.name for s in spectra]
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        for i in range(len(wavelengths)):
            row = [wavelengths[i]] + [s.reflectance[i] for s in spectra]
            writer.writerow(row)


def build_library(wavelengths: List[float]) -> Dict[str, Spectrum]:
    minerals: Dict[str, Spectrum] = {}

    # Feldspars
    minerals["plagioclase"] = make_mineral_spectrum(
        "plagioclase", wavelengths, low_reflectance=0.35, high_reflectance=0.55,
        dips=[(1.25, 0.08, 0.02)]
    )
    minerals["alkali_feldspar"] = make_mineral_spectrum(
        "alkali_feldspar", wavelengths, low_reflectance=0.45, high_reflectance=0.65,
        dips=[(1.26, 0.08, 0.015)]
    )

    # Mafic silicates
    minerals["pyroxene"] = make_mineral_spectrum(
        "pyroxene", wavelengths, low_reflectance=0.20, high_reflectance=0.35,
        dips=[(0.97, 0.06, 0.08), (2.00, 0.08, 0.06)]
    )
    minerals["olivine"] = make_mineral_spectrum(
        "olivine", wavelengths, low_reflectance=0.22, high_reflectance=0.36,
        dips=[(1.05, 0.20, 0.12)]
    )
    minerals["hornblende"] = make_mineral_spectrum(
        "hornblende", wavelengths, low_reflectance=0.25, high_reflectance=0.40,
        dips=[(1.00, 0.07, 0.05), (2.32, 0.06, 0.05)]
    )
    minerals["biotite"] = make_mineral_spectrum(
        "biotite", wavelengths, low_reflectance=0.23, high_reflectance=0.38,
        dips=[(1.00, 0.06, 0.06), (2.35, 0.06, 0.05)]
    )

    # Accessory / oxides
    minerals["quartz"] = make_mineral_spectrum(
        "quartz", wavelengths, low_reflectance=0.55, high_reflectance=0.65, dips=[]
    )
    minerals["chromite"] = make_mineral_spectrum(
        "chromite", wavelengths, low_reflectance=0.08, high_reflectance=0.15, dips=[]
    )
    minerals["hematite"] = make_mineral_spectrum(
        "hematite", wavelengths, low_reflectance=0.30, high_reflectance=0.50,
        dips=[(0.55, 0.03, 0.15), (0.86, 0.06, 0.18)]
    )
    minerals["goethite"] = make_mineral_spectrum(
        "goethite", wavelengths, low_reflectance=0.28, high_reflectance=0.48,
        dips=[(0.48, 0.03, 0.12), (0.93, 0.05, 0.15)]
    )

    # Hydrothermal alteration / clays and hydration
    minerals["illite"] = make_mineral_spectrum(
        "illite", wavelengths, low_reflectance=0.40, high_reflectance=0.55,
        dips=[(1.41, 0.04, 0.10), (1.91, 0.06, 0.12), (2.20, 0.05, 0.08)]
    )
    minerals["chlorite"] = make_mineral_spectrum(
        "chlorite", wavelengths, low_reflectance=0.30, high_reflectance=0.45,
        dips=[(1.40, 0.04, 0.08), (1.90, 0.06, 0.10), (2.33, 0.05, 0.06)]
    )
    minerals["water_film"] = make_mineral_spectrum(
        "water_film", wavelengths, low_reflectance=0.0, high_reflectance=0.0,
        dips=[(1.41, 0.04, 0.25), (1.91, 0.06, 0.30)]
    )

    return minerals


def build_rock_endmembers(wavelengths: List[float], minerals: Dict[str, Spectrum]) -> Dict[str, Spectrum]:
    rocks: Dict[str, Spectrum] = {}

    # BXF (andesitic to basaltic volcanic breccias / tuffs), dominated by plagioclase + pyroxene + hornblende
    bxf_mix = [
        (minerals["plagioclase"], 0.50),
        (minerals["pyroxene"], 0.25),
        (minerals["hornblende"], 0.15),
        (minerals["olivine"], 0.05),
        (minerals["biotite"], 0.05),
    ]
    rocks["BXF"] = mix_spectra(wavelengths, bxf_mix, name="BXF")

    # Diorite: plagioclase + hornblende + biotite, minor pyroxene, minimal quartz
    diorite_mix = [
        (minerals["plagioclase"], 0.60),
        (minerals["hornblende"], 0.20),
        (minerals["biotite"], 0.15),
        (minerals["pyroxene"], 0.04),
        (minerals["quartz"], 0.01),
    ]
    rocks["Diorite"] = mix_spectra(wavelengths, diorite_mix, name="Diorite")

    # Monzonite: plagioclase + alkali feldspar + biotite + hornblende, minor quartz
    monzonite_mix = [
        (minerals["plagioclase"], 0.35),
        (minerals["alkali_feldspar"], 0.35),
        (minerals["biotite"], 0.15),
        (minerals["hornblende"], 0.10),
        (minerals["quartz"], 0.05),
    ]
    rocks["Monzonite"] = mix_spectra(wavelengths, monzonite_mix, name="Monzonite")

    # Picrite: ultramafic, olivine + pyroxene + chromite (dark)
    picrite_mix = [
        (minerals["olivine"], 0.50),
        (minerals["pyroxene"], 0.40),
        (minerals["chromite"], 0.10),
    ]
    rocks["Picrite"] = mix_spectra(wavelengths, picrite_mix, name="Picrite")

    # Oxidized BXF variant (East Cave): add Fe-oxides + clay + hydration; reduce mafic content slightly
    bxf_ox_mix = [
        (minerals["plagioclase"], 0.45),
        (minerals["pyroxene"], 0.20),
        (minerals["hornblende"], 0.12),
        (minerals["olivine"], 0.03),
        (minerals["biotite"], 0.05),
        (minerals["hematite"], 0.06),
        (minerals["goethite"], 0.05),
        (minerals["illite"], 0.02),
        (minerals["water_film"], 0.02),
    ]
    rocks["BXF_Oxidized"] = mix_spectra(wavelengths, bxf_ox_mix, name="BXF_Oxidized")

    return rocks


def compute_area_lithology_fractions() -> Dict[str, Dict[str, float]]:
    # Volumes (m3) as provided
    west = {"BXF": 1656500, "Diorite": 26209, "Monzonite": 64839, "Picrite": 0}
    east = {"BXF": 3473800, "Diorite": 879910, "Monzonite": 57703, "Picrite": 9202}
    b3 = {"BXF": 1256800, "Diorite": 60764, "Monzonite": 14673, "Picrite": 0}

    def normalize(vols: Dict[str, float]) -> Dict[str, float]:
        total = sum(vols.values())
        return {k: (v / total if total > 0 else 0.0) for k, v in vols.items()}

    return {
        "West": normalize(west),
        "East": normalize(east),
        "B3": normalize(b3),
    }


def compute_composites(
    wavelengths: List[float],
    rocks: Dict[str, Spectrum],
    area_fracs: Dict[str, Dict[str, float]],
) -> Dict[str, Spectrum]:
    composites: Dict[str, Spectrum] = {}

    # Area tonnage weights (fraction of mined tons)
    area_ton_weights = {"West": 0.13, "East": 0.67, "MRC": 0.06, "B3": 0.14}

    # Default lithology mix for MRC (not provided): use overall lithology proportions (85/13/2/0)
    mrc_litho = {"BXF": 0.85, "Diorite": 0.13, "Monzonite": 0.02, "Picrite": 0.00}

    # Build per-area composites
    for area_name, litho in area_fracs.items():
        components: List[Tuple[Spectrum, float]] = []
        # Use oxidized BXF only for East-specific variant elsewhere
        components.append((rocks["BXF"], litho.get("BXF", 0.0)))
        components.append((rocks["Diorite"], litho.get("Diorite", 0.0)))
        components.append((rocks["Monzonite"], litho.get("Monzonite", 0.0)))
        components.append((rocks["Picrite"], litho.get("Picrite", 0.0)))
        composites[f"{area_name}_Composite"] = mix_spectra(wavelengths, components, f"{area_name}_Composite")

    # East oxidized composite: replace BXF with oxidized variant for the BXF fraction only
    east_litho = area_fracs["East"]
    east_components_ox: List[Tuple[Spectrum, float]] = []
    east_components_ox.append((rocks["BXF_Oxidized"], east_litho.get("BXF", 0.0)))
    east_components_ox.append((rocks["Diorite"], east_litho.get("Diorite", 0.0)))
    east_components_ox.append((rocks["Monzonite"], east_litho.get("Monzonite", 0.0)))
    east_components_ox.append((rocks["Picrite"], east_litho.get("Picrite", 0.0)))
    composites["East_Composite_Oxidized"] = mix_spectra(wavelengths, east_components_ox, "East_Composite_Oxidized")

    # ROM default (no oxidation) using area tonnage weights and area composites
    rom_components: List[Tuple[Spectrum, float]] = []
    for area in ["West", "East", "B3"]:
        rom_components.append((composites[f"{area}_Composite"], area_ton_weights[area]))
    # MRC uses default lithology
    mrc_components = [
        (rocks["BXF"], mrc_litho["BXF"] * area_ton_weights["MRC"]),
        (rocks["Diorite"], mrc_litho["Diorite"] * area_ton_weights["MRC"]),
        (rocks["Monzonite"], mrc_litho["Monzonite"] * area_ton_weights["MRC"]),
        (rocks["Picrite"], mrc_litho["Picrite"] * area_ton_weights["MRC"]),
    ]
    rom_components.extend(mrc_components)
    composites["ROM_Overall"] = mix_spectra(wavelengths, rom_components, name="ROM_Overall")

    # ROM with East oxidation applied to East only
    rom_ox_components: List[Tuple[Spectrum, float]] = []
    rom_ox_components.append((composites["West_Composite"], area_ton_weights["West"]))
    rom_ox_components.append((composites["East_Composite_Oxidized"], area_ton_weights["East"]))
    rom_ox_components.append((composites["B3_Composite"], area_ton_weights["B3"]))
    rom_ox_components.extend(mrc_components)
    composites["ROM_With_East_Oxidized"] = mix_spectra(wavelengths, rom_ox_components, name="ROM_With_East_Oxidized")

    return composites


def main() -> None:
    out_dir = "/workspace/outputs/spectra"
    os.makedirs(out_dir, exist_ok=True)

    wavelengths = generate_wavelengths(0.40, 2.50, 0.005)

    # Mineral library
    minerals = build_library(wavelengths)

    # Rock endmembers
    rocks = build_rock_endmembers(wavelengths, minerals)

    # Write minerals
    mineral_spectra = [minerals[k] for k in sorted(minerals.keys())]
    write_csv(os.path.join(out_dir, "minerals_endmembers.csv"), mineral_spectra)

    # Write rocks
    rock_spectra = [rocks[k] for k in ["BXF", "BXF_Oxidized", "Diorite", "Monzonite", "Picrite"]]
    write_csv(os.path.join(out_dir, "rocks_endmembers.csv"), rock_spectra)

    # Area fractions and composites
    area_fracs = compute_area_lithology_fractions()
    composites = compute_composites(wavelengths, rocks, area_fracs)

    composite_spectra = [
        composites["West_Composite"],
        composites["East_Composite"],
        composites["East_Composite_Oxidized"],
        composites["B3_Composite"],
        composites["ROM_Overall"],
        composites["ROM_With_East_Oxidized"],
    ]
    write_csv(os.path.join(out_dir, "composites.csv"), composite_spectra)

    print(f"Wrote {len(mineral_spectra)} mineral spectra, {len(rock_spectra)} rock spectra, {len(composite_spectra)} composites to {out_dir}")


if __name__ == "__main__":
    main()