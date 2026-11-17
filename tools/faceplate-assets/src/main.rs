//! Pre-renders GoodLookin DRI knobs, switches and plates as PNG sprite sheets for the web UI.
//! Usage: faceplate-assets [--all] <layouts_dir> <out_dir>

use goodlookindri::color::Color;
use goodlookindri::design::{find_faceplate, KnobMode, FACEPLATES, KNOB_STYLES};
use goodlookindri::scene::{Cmd, Point, Rect, Scene};
use goodlookindri::switches::{self, PushStyle, RockerStyle, ToggleStyle, PUSH_NAMES, ROCKER_NAMES, TOGGLE_NAMES};
use goodlookindri::{draw, hardware, knob3d};
use goodlookindri_shell::raster::Framebuffer;
use serde_json::{json, Map, Value};
use std::collections::BTreeSet;
use std::path::Path;

const KNOB_FRAMES: usize = 64;
const KNOB_SIZE: usize = 128;
/// Knob radius inside the 128 px frame; the rest is room for the lean of tall knobs and the shadow.
const KNOB_RADIUS: f32 = 40.0;
/// DRI's own neutral cap colour (what `draw::knob` uses for an inactive control).
const ACCENT: Color = Color(0xff697170);
const SWITCH_SCALE: f32 = 2.0;
const PLATE: (usize, usize) = (760, 300);

const TOGGLES: [ToggleStyle; 3] = [ToggleStyle::Bat, ToggleStyle::Lever, ToggleStyle::Mini];
const ROCKERS: [RockerStyle; 4] = [RockerStyle::Lamp, RockerStyle::Classic, RockerStyle::Slim, RockerStyle::Neon];
const PUSHES: [PushStyle; 8] = [PushStyle::Square, PushStyle::Round, PushStyle::Dome, PushStyle::Chamfer,
    PushStyle::Neon, PushStyle::Flat, PushStyle::Arcade, PushStyle::Pill];

fn die(msg: String) -> ! {
    eprintln!("faceplate-assets: {msg}");
    std::process::exit(1);
}

fn slug(name: &str) -> String {
    name.chars().map(|c| if c.is_ascii_alphanumeric() { c.to_ascii_lowercase() } else { '-' }).collect()
}

fn replay(scene: &Scene, w: usize, h: usize, scale: f32, bg: u32) -> Vec<u32> {
    let mut fb = Framebuffer::new(w, h);
    fb.clear(Color(bg));
    fb.replay(scene, scale);
    fb.pixels
}

/// The framebuffer is opaque, so paint on black and on white and solve `out = c*a + bg*(1-a)`
/// for straight-alpha RGBA. Additive glows have no single alpha; the largest channel is used.
fn render_rgba(scene: &Scene, w: usize, h: usize, scale: f32) -> Vec<u8> {
    let (black, white) = (replay(scene, w, h, scale, 0), replay(scene, w, h, scale, 0xffffff));
    let mut out = Vec::with_capacity(w * h * 4);
    for (b, wh) in black.iter().zip(&white) {
        let ch = |p: &u32| [(p >> 16) as u8 as i32, (p >> 8) as u8 as i32, *p as u8 as i32];
        let (b, wh) = (ch(b), ch(wh));
        let a = (0..3).map(|i| 255 - (wh[i] - b[i])).max().unwrap().clamp(0, 255);
        for i in 0..3 { out.push(if a == 0 { 0 } else { (b[i] * 255 / a).min(255) as u8 }); }
        out.push(a as u8);
    }
    out
}

fn write_png(path: &Path, w: usize, h: usize, data: &[u8], colour: png::ColorType) {
    let file = std::fs::File::create(path).unwrap_or_else(|e| die(format!("{}: {e}", path.display())));
    let mut enc = png::Encoder::new(std::io::BufWriter::new(file), w as u32, h as u32);
    enc.set_color(colour);
    enc.set_depth(png::BitDepth::Eight);
    enc.set_compression(png::Compression::Best);
    let done = enc.write_header().and_then(|mut wr| wr.write_image_data(data));
    done.unwrap_or_else(|e| die(format!("{}: {e}", path.display())));
}

/// RGBA frames of equal size side by side. Warns when a frame has paint on its border (clipped).
fn write_strip(path: &Path, w: usize, h: usize, frames: &[Vec<u8>]) {
    let mut strip = vec![0u8; frames.len() * w * h * 4];
    for (i, f) in frames.iter().enumerate() {
        for y in 0..h {
            let dst = (y * frames.len() * w + i * w) * 4;
            strip[dst..dst + w * 4].copy_from_slice(&f[y * w * 4..(y + 1) * w * 4]);
        }
        let edge = (0..w).flat_map(|x| [(x, 0), (x, h - 1)]).chain((0..h).flat_map(|y| [(0, y), (w - 1, y)]))
            .map(|(x, y)| f[(y * w + x) * 4 + 3]).max().unwrap();
        if edge > 40 { eprintln!("warning: {} frame {i} touches the frame edge (alpha {edge})", path.display()); }
    }
    write_png(path, frames.len() * w, h, &strip, png::ColorType::Rgba);
}

fn write_json(path: &Path, v: &Value) {
    let text = serde_json::to_string_pretty(v).unwrap() + "\n";
    std::fs::write(path, text).unwrap_or_else(|e| die(format!("{}: {e}", path.display())));
}

/// Exactly the 47 catalogue codes, plus the 15 lit-only preset codes. No aliases or prefixes.
fn knob_known(name: &str) -> bool {
    KNOB_STYLES.iter().any(|s| s.code == name) || knob3d::PRESETS.contains(&name)
}

fn render_knob(name: &str, path: &Path) {
    draw::set_knob_scale(1.0);
    hardware::set_device_scale(1.0);
    let c = Point::new(KNOB_SIZE as f32 * 0.5, KNOB_SIZE as f32 * 0.5);
    let frames: Vec<Vec<u8>> = (0..KNOB_FRAMES).map(|i| {
        // DRI angles: 0 points up, negative is counter-clockwise. Frame 0 is the minimum.
        let angle = (-135.0 + 270.0 * i as f32 / (KNOB_FRAMES - 1) as f32).to_radians();
        let mut scene = Scene::new();
        draw::knob_named_mode(&mut scene, name, c, KNOB_RADIUS, angle, ACCENT, false, KnobMode::Lit);
        // A lit knob is one sprite preceded by its printed scale. The scale is dropped: its ink
        // depends on the plate and its sweep (288 deg) is not the 270 deg of these frames.
        if let Some(at) = scene.cmds.iter().position(|c| matches!(c, Cmd::Image { .. })) { scene.cmds.drain(..at); }
        render_rgba(&scene, KNOB_SIZE, KNOB_SIZE, 1.0)
    }).collect();
    assert!(frames[0] != frames[KNOB_FRAMES - 1], "knob {name}: first and last frame are identical");
    write_strip(path, KNOB_SIZE, KNOB_SIZE, &frames);
}

/// Family, frame size in scene units, and the painter of one state (false = off/released).
fn switch_spec(name: &str) -> Option<(&'static str, f32, f32, Box<dyn Fn(&mut Scene, bool)>)> {
    if let Some(i) = TOGGLE_NAMES.iter().position(|n| *n == name) {
        return Some(("toggle", 64.0, 108.0, Box::new(move |s, on|
            switches::draw_toggle(s, Rect::new(12.0, 16.0, 40.0, 70.0), on, TOGGLES[i], Color(0xfff2a31b)))));
    }
    if let Some(i) = ROCKER_NAMES.iter().position(|n| *n == name) {
        return Some(("rocker", 64.0, 92.0, Box::new(move |s, on|
            switches::draw_rocker(s, Rect::new(10.0, 11.0, 44.0, 70.0), on, Color(0xffff3b4a), ROCKERS[i], false, true))));
    }
    let i = PUSH_NAMES.iter().position(|n| *n == name)?;
    Some(("push", 128.0, 82.0, Box::new(move |s, on|
        switches::draw_push(s, Rect::new(24.0, 24.0, 80.0, 34.0), "", PUSHES[i], Color(0xff3f6fd0), on, on, true))))
}

fn render_switch(name: &str, path: &Path) -> Value {
    hardware::set_device_scale(SWITCH_SCALE);
    let (family, w, h, paint) = switch_spec(name).unwrap();
    let (pw, ph) = ((w * SWITCH_SCALE) as usize, (h * SWITCH_SCALE) as usize);
    let frames: Vec<Vec<u8>> = [false, true].iter().map(|&on| {
        let mut scene = Scene::new();
        paint(&mut scene, on);
        render_rgba(&scene, pw, ph, SWITCH_SCALE)
    }).collect();
    assert!(frames[0] != frames[1], "switch {name}: both states are identical");
    write_strip(path, pw, ph, &frames);
    json!({"states": 2, "w": pw, "h": ph, "family": family})
}

fn render_plate(name: &str, path: &Path) -> Value {
    hardware::set_device_scale(1.0);
    let p = find_faceplate(name).unwrap();
    let (top, bottom) = (Color(p.panel_top), Color(p.panel_bottom));
    let mut scene = Scene::new();
    let r = Rect::new(0.0, 0.0, PLATE.0 as f32, PLATE.1 as f32);
    // Square corners so the tile fills the frame; the page rounds its own panel.
    hardware::draw_panel_with(&mut scene, r, 0.0, top, bottom, p.finish, &goodlookindri::light::LightRig::studio());
    let rgb: Vec<u8> = replay(&scene, PLATE.0, PLATE.1, 1.0, p.bg_fill).iter()
        .flat_map(|px| [(px >> 16) as u8, (px >> 8) as u8, *px as u8]).collect();
    write_png(path, PLATE.0, PLATE.1, &rgb, png::ColorType::Rgb);
    let dark = hardware::use_dark_marks("auto", top.mix(bottom, 0.5));
    json!({"w": PLATE.0, "h": PLATE.1, "marks": if dark { "dark" } else { "light" },
           "text": format!("#{:06x}", p.text & 0xffffff)})
}

fn main() {
    let mut args: Vec<String> = std::env::args().skip(1).collect();
    let all = args.iter().any(|a| a == "--all");
    args.retain(|a| a != "--all");
    if args.len() != 2 { die("usage: faceplate-assets [--all] <layouts_dir> <out_dir>".into()); }
    let (layouts, out) = (Path::new(&args[0]), Path::new(&args[1]));

    let all_knobs: Vec<&str> = KNOB_STYLES.iter().map(|s| s.code).chain(knob3d::PRESETS).collect();
    let all_switches: Vec<&str> = TOGGLE_NAMES.iter().chain(&ROCKER_NAMES).chain(&PUSH_NAMES).copied().collect();
    let all_plates: Vec<&str> = FACEPLATES.iter().map(|p| p.code).collect();

    // styles.json first, so the list of valid names exists even when a layout is rejected.
    std::fs::create_dir_all(out).unwrap_or_else(|e| die(format!("{}: {e}", out.display())));
    let styles = json!({
        "knobs": KNOB_STYLES.iter().map(|s| s.code).collect::<Vec<_>>(),
        "knob_presets": knob3d::PRESETS,
        "knobs_without_lit_model": KNOB_STYLES.iter().map(|s| s.code)
            .filter(|c| knob3d::style_model(c).is_none()).collect::<Vec<_>>(),
        "switches": {"toggle": TOGGLE_NAMES, "rocker": ROCKER_NAMES, "push": PUSH_NAMES},
        "plates": all_plates,
    });
    write_json(&out.join("styles.json"), &styles);

    // Layouts are always validated, also with --all.
    let (mut knobs, mut sw, mut plates) = (BTreeSet::new(), BTreeSet::new(), BTreeSet::new());
    let mut files: Vec<_> = std::fs::read_dir(layouts).unwrap_or_else(|e| die(format!("{}: {e}", layouts.display())))
        .filter_map(|e| e.ok().map(|e| e.path())).filter(|p| p.extension().is_some_and(|x| x == "json")).collect();
    files.sort();
    let mut bad = Vec::new();
    for f in &files {
        let text = std::fs::read_to_string(f).unwrap_or_else(|e| die(format!("{}: {e}", f.display())));
        let v: Value = serde_json::from_str(&text).unwrap_or_else(|e| die(format!("{}: {e}", f.display())));
        match v["plate"].as_str() {
            Some(p) if find_faceplate(p).is_some() => { plates.insert(p.to_string()); }
            p => bad.push(format!("{}: unknown plate style {:?}", f.display(), p.unwrap_or("(missing)"))),
        }
        for c in v["controls"].as_array().map(Vec::as_slice).unwrap_or(&[]) {
            let (kind, style) = (c["kind"].as_str().unwrap_or(""), c["style"].as_str().unwrap_or(""));
            let key = c["key"].as_str().unwrap_or("?");
            match kind {
                "knob" | "stepped" if knob_known(style) => { knobs.insert(style.to_string()); }
                "toggle" if switch_spec(style).is_some() => { sw.insert(style.to_string()); }
                "knob" | "stepped" => bad.push(format!("{}: control {key:?}: unknown knob style {style:?}", f.display())),
                "toggle" => bad.push(format!("{}: control {key:?}: unknown switch style {style:?}", f.display())),
                _ => bad.push(format!("{}: control {key:?}: unknown kind {kind:?}", f.display())),
            }
        }
    }
    if !bad.is_empty() { die(format!("{}\n(valid names are in styles.json)", bad.join("\n"))); }
    if all {
        knobs = all_knobs.iter().map(|s| s.to_string()).collect();
        sw = all_switches.iter().map(|s| s.to_string()).collect();
        plates = all_plates.iter().map(|s| s.to_string()).collect();
    }

    for d in ["knobs", "switches", "plates"] {
        std::fs::create_dir_all(out.join(d)).unwrap_or_else(|e| die(format!("{}: {e}", out.display())));
    }
    let mut manifest = json!({"knobs": {}, "switches": {}, "plates": {}});
    let mut emit = |kind: &str, name: &str, mut entry: Map<String, Value>| {
        entry.insert("file".into(), json!(format!("{kind}/{}.png", slug(name))));
        manifest[kind][name] = Value::Object(entry);
    };
    let file = |kind: &str, name: &str| out.join(kind).join(format!("{}.png", slug(name)));
    for (kind, set) in [("knobs", &knobs), ("switches", &sw), ("plates", &plates)] {
        let slugs: BTreeSet<String> = set.iter().map(|n| slug(n)).collect();
        assert_eq!(slugs.len(), set.len(), "{kind}: two style names share a file name");
    }
    for name in &knobs {
        render_knob(name, &file("knobs", name));
        let lit = knob3d::PRESETS.contains(&name.as_str()) || knob3d::style_model(name).is_some();
        let entry = json!({"frames": KNOB_FRAMES, "size": KNOB_SIZE, "knob": KNOB_RADIUS * 2.0, "lit": lit});
        emit("knobs", name, entry.as_object().unwrap().clone());
    }
    for name in &sw {
        let entry = render_switch(name, &file("switches", name));
        emit("switches", name, entry.as_object().unwrap().clone());
    }
    for name in &plates {
        let entry = render_plate(name, &file("plates", name));
        emit("plates", name, entry.as_object().unwrap().clone());
    }

    write_json(&out.join("manifest.json"), &manifest);
    println!("{} layouts; rendered {} knobs, {} switches, {} plates into {}",
        files.len(), knobs.len(), sw.len(), plates.len(), out.display());
}
