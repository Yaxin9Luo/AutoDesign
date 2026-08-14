import type { Artifact } from "./types";

let _id = 0;
export const nextId = (prefix = "id") => `${prefix}_${++_id}_${Date.now().toString(36)}`;

export type ClientDemoArtifact = Artifact & { client_demo: true };

export function isClientDemoArtifact(
  artifact: Artifact | null | undefined,
): artifact is ClientDemoArtifact {
  return (artifact as ClientDemoArtifact | undefined)?.client_demo === true;
}

/**
 * Sample poster — mirrors what `autodesign` produces for a 3:4 brief.
 * 5 layers (background + 3 text + 1 shape) on a 1200x1600 canvas.
 */
export const samplePoster = (): ClientDemoArtifact => ({
  artifact_id: nextId("art"),
  client_demo: true,
  name: "Sample Poster — Open Source Launch",
  artifact_type: "poster",
  canvas: { w: 1200, h: 1600, background: "#f4ede2" },
  layers: [
    {
      layer_id: "lyr_bg",
      name: "Background",
      kind: "background",
      z_index: 0,
      fill_color: "#f4ede2",
      bbox: { x: 0, y: 0, w: 1200, h: 1600 },
      visible: true,
      locked: true,
    },
    {
      layer_id: "lyr_accent",
      name: "Accent block",
      kind: "shape",
      shape_kind: "rect",
      z_index: 1,
      bbox: { x: 80, y: 1280, w: 220, h: 16 },
      fill_color: "#1f1f1f",
      visible: true,
    },
    {
      layer_id: "lyr_eyebrow",
      name: "Eyebrow",
      kind: "text",
      z_index: 2,
      bbox: { x: 80, y: 200, w: 1040, h: 60 },
      text: "OPEN · DESIGN · 2026",
      font_family: "Inter",
      font_size_px: 28,
      font_weight: 500,
      letter_spacing: 6,
      align: "left",
      effects: { fill: "#6b6b6b" },
      visible: true,
    },
    {
      layer_id: "lyr_title",
      name: "Title",
      kind: "text",
      z_index: 3,
      bbox: { x: 80, y: 320, w: 1040, h: 540 },
      text: "Design,\nin a conversation.",
      font_family: "Playfair Display",
      font_size_px: 168,
      font_weight: 600,
      line_height: 1.02,
      letter_spacing: -2,
      align: "left",
      effects: { fill: "#1f1f1f" },
      visible: true,
    },
    {
      layer_id: "lyr_body",
      name: "Subtitle",
      kind: "text",
      z_index: 4,
      bbox: { x: 80, y: 920, w: 980, h: 220 },
      text: "An open-source agent that turns a sentence into editable HTML, PPTX, PSD, and SVG. Bring your own model. Keep your own files.",
      font_family: "Inter",
      font_size_px: 36,
      font_weight: 400,
      line_height: 1.45,
      align: "left",
      effects: { fill: "#2a2a2a" },
      visible: true,
    },
    {
      layer_id: "lyr_footer",
      name: "Footer",
      kind: "text",
      z_index: 5,
      bbox: { x: 80, y: 1320, w: 1040, h: 60 },
      text: "github.com/Yaxin9Luo/AutoDesign",
      font_family: "JetBrains Mono",
      font_size_px: 24,
      font_weight: 500,
      align: "left",
      effects: { fill: "#1f1f1f" },
      visible: true,
    },
  ],
});

/**
 * Editable slide stack — three 16:9 slides on one tall canvas. This is
 * deliberately layer-based (no native HTML/PPTX file) so every text and
 * shape edit is immediate and safe to dogfood without an agent run.
 */
export const sampleSlides = (): Artifact => ({
  artifact_id: nextId("art"),
  name: "Editable Slides — Reliability Brief",
  artifact_type: "deck",
  canvas: { w: 1280, h: 2360, background: "#ebe7dc" },
  layers: [
    {
      layer_id: "slides_bg",
      name: "Canvas background",
      kind: "background",
      z_index: 0,
      fill_color: "#ebe7dc",
      bbox: { x: 0, y: 0, w: 1280, h: 2360 },
      visible: true,
      locked: true,
    },
    ...slideFrame("s1", 40, "#fbf8ef", 1),
    ...slideFrame("s2", 820, "#f7f1e2", 10),
    ...slideFrame("s3", 1600, "#fbf8ef", 20),
    textLayer("s1_kicker", "Section label", 2, 96, 130, 840, 36, "Field Notes · Q2 2026", 22, 500, "#6b6256", 4),
    textLayer("s1_title", "Slide 1 title", 3, 96, 215, 850, 190, "Reliable agents need visible checkpoints.", 72, 650, "#17130f", -1.5, 0.96),
    textLayer("s1_body", "Slide 1 body", 4, 100, 470, 760, 120, "Use this editable slide demo to test title hierarchy, copy edits, layer movement, and shape composition without running the agent.", 26, 400, "#3d352d", 0, 1.36),
    shapeLayer("s1_rule", "Accent rule", 5, 96, 635, 250, 10, "#1f6f4a"),
    shapeLayer("s1_badge", "Status badge", 6, 900, 142, 230, 56, "#1d3a2f"),
    textLayer("s1_badge_text", "Badge text", 7, 928, 158, 180, 28, "EDITABLE", 18, 700, "#fbf8ef", 2),

    textLayer("s2_kicker", "Metric label", 11, 96, 920, 760, 34, "Workflow Observability", 22, 500, "#6b6256", 4),
    textLayer("s2_title", "Slide 2 title", 12, 96, 990, 580, 120, "Before / After", 54, 650, "#17130f", -1, 1),
    shapeLayer("s2_bar_bad", "Before bar", 13, 125, 1288, 220, 72, "#8e3b32"),
    shapeLayer("s2_bar_good", "After bar", 14, 125, 1415, 640, 72, "#1f6f4a"),
    textLayer("s2_bad_num", "Before metric", 15, 380, 1282, 220, 80, "22%", 56, 700, "#8e3b32", -1),
    textLayer("s2_good_num", "After metric", 16, 805, 1408, 220, 80, "90%", 56, 700, "#1f6f4a", -1),
    textLayer("s2_note", "Metric note", 17, 96, 1152, 800, 78, "Swap numbers, recolor bars, or move the chart blocks to test designer-style iteration.", 24, 400, "#3d352d", 0, 1.35),

    textLayer("s3_kicker", "Closing label", 21, 96, 1705, 760, 34, "Decision Slide", 22, 500, "#6b6256", 4),
    textLayer("s3_title", "Slide 3 title", 22, 96, 1790, 820, 150, "Ship fewer black boxes.", 70, 650, "#17130f", -1.4, 0.98),
    textLayer("s3_body", "Slide 3 body", 23, 100, 1988, 700, 100, "A good agent UI should show progress, preserve editability, and make failures recoverable.", 27, 400, "#3d352d", 0, 1.36),
    shapeLayer("s3_chip_1", "Chip 1", 24, 96, 2148, 190, 54, "#1d3a2f"),
    textLayer("s3_chip_1_text", "Chip 1 text", 25, 126, 2163, 130, 24, "TRACE", 17, 700, "#fbf8ef", 2),
    shapeLayer("s3_chip_2", "Chip 2", 26, 310, 2148, 220, 54, "#d8d1bf"),
    textLayer("s3_chip_2_text", "Chip 2 text", 27, 342, 2163, 160, 24, "RECOVER", 17, 700, "#17130f", 2),
  ],
});

/**
 * Editable landing page — a long-page mockup made of frontend layers so
 * hero, cards, stats, and CTA copy can all be edited immediately.
 */
export const sampleLandingPage = (): Artifact => ({
  artifact_id: nextId("art"),
  name: "Editable Landing — AgentOps",
  artifact_type: "landing",
  canvas: { w: 1440, h: 2200, background: "#f5f1e8" },
  layers: [
    {
      layer_id: "landing_bg",
      name: "Page background",
      kind: "background",
      z_index: 0,
      fill_color: "#f5f1e8",
      bbox: { x: 0, y: 0, w: 1440, h: 2200 },
      visible: true,
      locked: true,
    },
    textLayer("lp_brand", "Brand", 1, 96, 64, 220, 36, "AgentOps", 26, 700, "#17130f", 0),
    textLayer("lp_nav", "Navigation", 2, 780, 68, 540, 30, "Product     Pricing     Docs     Sign in", 18, 500, "#5f574e", 0),
    textLayer("lp_eyebrow", "Hero eyebrow", 3, 96, 210, 720, 36, "LOCAL-FIRST DESIGN WORKBENCH", 18, 700, "#1f6f4a", 3),
    textLayer("lp_headline", "Hero headline", 4, 96, 290, 760, 250, "Design agents that stay editable.", 86, 650, "#17130f", -1.8, 0.95),
    textLayer("lp_subhead", "Hero subhead", 5, 100, 575, 680, 120, "Turn research briefs into posters, slides, landing pages, and videos. Keep the result inspectable, editable, and grounded in your own files.", 28, 400, "#3d352d", 0, 1.34),
    shapeLayer("lp_primary_cta", "Primary CTA", 6, 100, 745, 215, 64, "#17130f"),
    textLayer("lp_primary_cta_text", "Primary CTA text", 7, 132, 765, 150, 24, "Start designing", 18, 700, "#fbf8ef", 0),
    shapeLayer("lp_secondary_cta", "Secondary CTA", 8, 340, 745, 210, 64, "transparent", "#bdb5a6", 2),
    textLayer("lp_secondary_cta_text", "Secondary CTA text", 9, 372, 765, 146, 24, "View examples", 18, 700, "#17130f", 0),
    shapeLayer("lp_hero_panel", "Hero visual panel", 10, 880, 240, 420, 520, "#1d3a2f"),
    shapeLayer("lp_hero_card", "Hero floating card", 11, 805, 390, 390, 210, "#fbf8ef"),
    textLayer("lp_hero_card_title", "Hero card title", 12, 845, 430, 300, 68, "Run timeline", 28, 650, "#17130f", -0.4),
    textLayer("lp_hero_card_body", "Hero card body", 13, 846, 515, 290, 64, "Designer · Critic · Composer · Export", 20, 400, "#5f574e", 0, 1.32),

    shapeLayer("lp_band", "Feature band", 14, 0, 920, 1440, 560, "#fffaf0"),
    textLayer("lp_section_title", "Feature heading", 15, 96, 1028, 660, 120, "Everything visible, nothing trapped.", 54, 650, "#17130f", -1, 1.02),
    ...featureCard("lp_card_1", 96, 1220, "01", "Editable outputs", "Text, layout, and layers remain inspectable after generation.", 20),
    ...featureCard("lp_card_2", 525, 1220, "02", "Recoverable runs", "Progress cards make stalls and retries explicit.", 30),
    ...featureCard("lp_card_3", 954, 1220, "03", "Bring your models", "Route each agent role to the provider that fits.", 40),

    textLayer("lp_metric_1", "Metric 1", 50, 120, 1625, 220, 80, "8", 72, 700, "#1f6f4a", -1),
    textLayer("lp_metric_1_label", "Metric 1 label", 51, 124, 1710, 270, 46, "agent roles wired into one local workflow", 22, 400, "#3d352d", 0, 1.24),
    textLayer("lp_metric_2", "Metric 2", 52, 530, 1625, 220, 80, "3", 72, 700, "#1f6f4a", -1),
    textLayer("lp_metric_2_label", "Metric 2 label", 53, 534, 1710, 270, 46, "editable artifact formats for fast iteration", 22, 400, "#3d352d", 0, 1.24),
    textLayer("lp_metric_3", "Metric 3", 54, 940, 1625, 260, 80, "0", 72, 700, "#1f6f4a", -1),
    textLayer("lp_metric_3_label", "Metric 3 label", 55, 944, 1710, 300, 46, "cloud accounts required for local demos", 22, 400, "#3d352d", 0, 1.24),

    shapeLayer("lp_final_cta", "Final CTA band", 60, 96, 1885, 1248, 250, "#1d3a2f"),
    textLayer("lp_final_title", "Final CTA title", 61, 150, 1945, 1000, 54, "Keep designing after generation.", 36, 650, "#fbf8ef", -0.6, 1.05),
    textLayer("lp_final_body", "Final CTA body", 62, 152, 2028, 850, 46, "Use this demo to test long-page copy, hierarchy, and CTA spacing.", 22, 400, "#d8d1bf", 0),
  ],
});

/**
 * Editable video demo — four 16:9 scenes on one tall canvas. Each scene
 * is a regular layer frame so the existing canvas editor can select,
 * move, resize, restyle, and replace the visible scene content.
 */
export const sampleVideo = (): Artifact => ({
  artifact_id: nextId("art"),
  name: "Editable Video — Product Walkthrough",
  artifact_type: "video",
  canvas: { w: 1280, h: 2960, background: "#17130f" },
  video_project: {
    fps: 30,
    duration_s: 18,
    scenes: [
      { scene_id: "scene_1", name: "Opening", frame_layer_id: "v1_frame", duration_s: 4.5, transition: "fade" },
      { scene_id: "scene_2", name: "Problem", frame_layer_id: "v2_frame", duration_s: 4, transition: "wipe" },
      { scene_id: "scene_3", name: "Workflow", frame_layer_id: "v3_frame", duration_s: 5, transition: "wipe" },
      { scene_id: "scene_4", name: "Close", frame_layer_id: "v4_frame", duration_s: 4.5, transition: "fade" },
    ],
  },
  layers: [
    {
      layer_id: "video_bg",
      name: "Video canvas background",
      kind: "background",
      z_index: 0,
      fill_color: "#17130f",
      bbox: { x: 0, y: 0, w: 1280, h: 2960 },
      visible: true,
      locked: true,
    },
    ...videoFrame("v1", 40, "#f6efe3", 1),
    textLayer("v1_label", "Scene label", 3, 96, 112, 560, 34, "LOCAL-FIRST VIDEO EDITING", 18, 700, "#176448", 2.4),
    textLayer("v1_title", "Opening title", 4, 96, 185, 640, 156, "Editable videos start as scenes.", 62, 700, "#17130f", -1.2, 0.98),
    textLayer("v1_body", "Opening body", 5, 100, 382, 560, 78, "Use the same canvas tools to edit text, move visual blocks, and render a real MP4.", 22, 400, "#51483e", 0, 1.35),
    imageLayer("v1_visual", "Opening visual", 6, 780, 150, 360, 360, svgPanel("Video scene", "#176448", "#f6efe3")),
    shapeLayer("v1_chip", "Format chip", 7, 100, 560, 210, 48, "#17130f"),
    textLayer("v1_chip_text", "Format chip text", 8, 130, 574, 150, 22, "1920 x 1080", 15, 700, "#f6efe3", 1.2),

    ...videoFrame("v2", 775, "#fffaf0", 20),
    textLayer("v2_label", "Scene label", 22, 96, 848, 420, 34, "WHY IT MATTERS", 18, 700, "#92342e", 2.4),
    textLayer("v2_title", "Problem title", 23, 96, 920, 620, 120, "MP4-only output traps edits.", 52, 700, "#17130f", -1, 1),
    textLayer("v2_body", "Problem body", 24, 100, 1084, 620, 100, "The right model is editable scenes first, rendered video second. MP4 becomes an export, not the source of truth.", 24, 400, "#51483e", 0, 1.34),
    shapeLayer("v2_card_1", "Problem card", 25, 760, 902, 330, 130, "#f4e4da", "#e2c3b7", 1),
    textLayer("v2_card_1_text", "Problem card text", 26, 795, 940, 250, 52, "Timeline locked after export", 24, 650, "#92342e", -0.3, 1.08),
    shapeLayer("v2_card_2", "Solution card", 27, 760, 1084, 330, 130, "#e7f0e7", "#bfd2c4", 1),
    textLayer("v2_card_2_text", "Solution card text", 28, 795, 1122, 250, 52, "Scene layers stay editable", 24, 650, "#176448", -0.3, 1.08),

    ...videoFrame("v3", 1510, "#f5f1e8", 40),
    textLayer("v3_label", "Scene label", 42, 96, 1582, 360, 34, "WORKFLOW", 18, 700, "#176448", 2.4),
    textLayer("v3_title", "Workflow title", 43, 96, 1650, 620, 108, "Edit scenes. Render when ready.", 50, 700, "#17130f", -1, 1),
    shapeLayer("v3_step_1", "Step 1", 44, 120, 1865, 250, 150, "#fbf8ef", "#d8d1bf", 1),
    textLayer("v3_step_1_num", "Step 1 number", 45, 150, 1895, 70, 40, "01", 28, 700, "#176448", 1.4),
    textLayer("v3_step_1_text", "Step 1 text", 46, 150, 1952, 165, 42, "Edit layer content", 21, 650, "#17130f", -0.2, 1.05),
    shapeLayer("v3_step_2", "Step 2", 47, 500, 1865, 250, 150, "#fbf8ef", "#d8d1bf", 1),
    textLayer("v3_step_2_num", "Step 2 number", 48, 530, 1895, 70, 40, "02", 28, 700, "#176448", 1.4),
    textLayer("v3_step_2_text", "Step 2 text", 49, 530, 1952, 165, 42, "Tune scene duration", 21, 650, "#17130f", -0.2, 1.05),
    shapeLayer("v3_step_3", "Step 3", 50, 880, 1865, 250, 150, "#fbf8ef", "#d8d1bf", 1),
    textLayer("v3_step_3_num", "Step 3 number", 51, 910, 1895, 70, 40, "03", 28, 700, "#176448", 1.4),
    textLayer("v3_step_3_text", "Step 3 text", 52, 910, 1952, 165, 42, "Render MP4", 21, 650, "#17130f", -0.2, 1.05),

    ...videoFrame("v4", 2245, "#17251f", 60),
    textLayer("v4_label", "Scene label", 62, 96, 2320, 360, 34, "EXPORT", 18, 700, "#d8d1bf", 2.4),
    textLayer("v4_title", "Closing title", 63, 96, 2395, 720, 140, "Keep the project editable after render.", 56, 700, "#fbf8ef", -1.1, 1),
    textLayer("v4_body", "Closing body", 64, 100, 2572, 660, 88, "The latest MP4 is a preview and deliverable. The scene layers stay live for the next pass.", 24, 400, "#d8d1bf", 0, 1.34),
    shapeLayer("v4_button", "Render button", 65, 100, 2730, 210, 54, "#fbf8ef"),
    textLayer("v4_button_text", "Render button text", 66, 130, 2746, 150, 22, "Render again", 16, 700, "#17251f", 0),
  ],
});

function slideFrame(prefix: string, y: number, fill: string, z: number) {
  return [
    shapeLayer(`${prefix}_paper`, `${prefix.toUpperCase()} paper`, z, 40, y, 1200, 720, fill),
    textLayer(`${prefix}_num`, `${prefix.toUpperCase()} number`, z + 1, 1120, y + 48, 70, 28, prefix.slice(1).padStart(2, "0"), 18, 700, "#6b6256", 2),
  ];
}

function videoFrame(prefix: string, y: number, fill: string, z: number) {
  return [
    {
      ...shapeLayer(`${prefix}_frame`, `${prefix.toUpperCase()} scene frame`, z, 40, y, 1200, 675, fill),
      locked: true,
    },
    textLayer(`${prefix}_num`, `${prefix.toUpperCase()} scene number`, z + 1, 1120, y + 42, 70, 28, prefix.slice(1).padStart(2, "0"), 18, 700, "#6b6256", 2),
  ];
}

function featureCard(
  prefix: string,
  x: number,
  y: number,
  num: string,
  title: string,
  body: string,
  z: number,
) {
  return [
    shapeLayer(`${prefix}_box`, `${title} card`, z, x, y, 330, 210, "#f5f1e8", "#d8d1bf", 1),
    textLayer(`${prefix}_num`, `${title} number`, z + 1, x + 28, y + 28, 80, 34, num, 24, 700, "#1f6f4a", 1),
    textLayer(`${prefix}_title`, `${title} title`, z + 2, x + 28, y + 76, 260, 34, title, 25, 650, "#17130f", -0.3),
    textLayer(`${prefix}_body`, `${title} body`, z + 3, x + 28, y + 125, 260, 58, body, 19, 400, "#5f574e", 0, 1.28),
  ];
}

function shapeLayer(
  layer_id: string,
  name: string,
  z_index: number,
  x: number,
  y: number,
  w: number,
  h: number,
  fill_color: string,
  stroke_color?: string,
  stroke_width?: number,
): Artifact["layers"][number] {
  return {
    layer_id,
    name,
    kind: "shape",
    shape_kind: "rect",
    z_index,
    bbox: { x, y, w, h },
    fill_color,
    stroke_color,
    stroke_width,
    visible: true,
  };
}

function textLayer(
  layer_id: string,
  name: string,
  z_index: number,
  x: number,
  y: number,
  w: number,
  h: number,
  text: string,
  font_size_px: number,
  font_weight: number,
  fill: string,
  letter_spacing = 0,
  line_height = 1.2,
): Artifact["layers"][number] {
  return {
    layer_id,
    name,
    kind: "text",
    z_index,
    bbox: { x, y, w, h },
    text,
    font_family: "Inter",
    font_size_px,
    font_weight,
    line_height,
    letter_spacing,
    align: "left",
    effects: { fill },
    visible: true,
  };
}

function imageLayer(
  layer_id: string,
  name: string,
  z_index: number,
  x: number,
  y: number,
  w: number,
  h: number,
  src: string,
): Artifact["layers"][number] {
  return {
    layer_id,
    name,
    kind: "image",
    z_index,
    bbox: { x, y, w, h },
    src,
    fit: "contain",
    object_position: { x: 0.5, y: 0.5 },
    corner_radius: 28,
    visible: true,
  };
}

function svgPanel(label: string, fg: string, bg: string) {
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 720 720"><rect width="720" height="720" rx="56" fill="${bg}"/><circle cx="360" cy="268" r="116" fill="${fg}" opacity=".16"/><path d="M272 260h176M272 326h176M272 392h116" fill="none" stroke="${fg}" stroke-width="26" stroke-linecap="round"/><text x="360" y="530" text-anchor="middle" font-family="Inter,Arial,sans-serif" font-size="48" font-weight="700" fill="${fg}">${label}</text></svg>`;
  return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
}
