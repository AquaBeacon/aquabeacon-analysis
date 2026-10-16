// Camera calibration checkerboard for a Bambu Lab X1-Carbon + AMS.
// Export part="black" and part="white" separately as STL, then import both
// together into Bambu Studio as one object with multiple parts.

/* [Pattern] */
square_mm = 40;          // Checker square edge length in mm
columns = 5;             // Number of squares across
rows = 6;                // Number of squares down

/* [Print layers] */
layer_mm = 0.20;         // Slice at this layer height
base_layers = 9;         // Black base: 1.8 mm at 0.20 mm layers
face_layers = 2;         // White material: only 2 top layers (0.4 mm)

/* [Export] */
part = "both";           // [both,black,white]

base_mm = base_layers * layer_mm;
face_mm = face_layers * layer_mm;

assert(square_mm > 0);
assert(columns >= 2 && rows >= 2);
assert(base_layers >= 3 && face_layers >= 1);

module tile(col, row) {
    translate([col * square_mm, row * square_mm, base_mm])
        cube([square_mm, square_mm, face_mm]);
}

module black_body() {
    // Contiguous base; black face squares fill the other half of the top layer.
    union() {
        cube([columns * square_mm, rows * square_mm, base_mm]);
        for (row = [0:rows-1], col = [0:columns-1])
            if ((row + col) % 2 == 0) tile(col, row);
    }
}

module white_face() {
    // Only the visible light squares consume white filament.
    for (row = [0:rows-1], col = [0:columns-1])
        if ((row + col) % 2 == 1) tile(col, row);
}

if (part == "black") black_body();
else if (part == "white") white_face();
else if (part == "both") {
    color("black") black_body();
    color("white") white_face();
} else assert(false, "part must be both, black, or white");
