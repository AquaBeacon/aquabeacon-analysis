"""Bundle the two checkerboard STL meshes as one colored 3MF assembly."""

from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import trimesh

ROOT = Path(__file__).resolve().parent
NAME = "checkerboard_40mm"
CORE = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"


def main() -> None:
    scene = trimesh.Scene()
    for color in ("black", "white"):
        mesh = trimesh.load(ROOT / f"{NAME}_{color}.stl", force="mesh")
        scene.add_geometry(mesh, geom_name=color, node_name=color)

    package = BytesIO(scene.export(file_type="3mf"))
    with ZipFile(package) as source:
        files = {item.filename: source.read(item.filename) for item in source.infolist()}

    ET.register_namespace("", CORE)
    model = ET.fromstring(files["3D/3dmodel.model"])
    resources = model.find(f"{{{CORE}}}resources")
    build = model.find(f"{{{CORE}}}build")
    assert resources is not None and build is not None
    objects = resources.findall(f"{{{CORE}}}object")
    assert len(objects) == 2

    materials = ET.Element(f"{{{CORE}}}basematerials", {"id": "3"})
    ET.SubElement(materials, f"{{{CORE}}}base", {"name": "Black", "displaycolor": "#101010"})
    ET.SubElement(materials, f"{{{CORE}}}base", {"name": "White", "displaycolor": "#F8F8F8"})
    resources.insert(0, materials)

    for index, obj in enumerate(objects):
        obj.set("pid", "3")
        obj.set("pindex", str(index))

    assembly = ET.SubElement(
        resources,
        f"{{{CORE}}}object",
        {"id": "4", "name": "40 mm checkerboard", "type": "model"},
    )
    components = ET.SubElement(assembly, f"{{{CORE}}}components")
    for obj in objects:
        ET.SubElement(components, f"{{{CORE}}}component", {"objectid": obj.get("id", "")})
    build.clear()
    ET.SubElement(build, f"{{{CORE}}}item", {"objectid": "4"})

    files["3D/3dmodel.model"] = ET.tostring(model, encoding="utf-8", xml_declaration=True)
    output = ROOT / f"{NAME}.3mf"
    with ZipFile(output, "w") as target:
        for name, data in files.items():
            target.writestr(name, data)
    print(output)


if __name__ == "__main__":
    main()
