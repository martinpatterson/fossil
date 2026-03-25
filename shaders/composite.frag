#version 330 core

uniform sampler2D u_fossil;
uniform sampler2D u_persistence;
uniform sampler2D u_silhouette;
uniform sampler2D u_depth_persist;
uniform float     u_fade_rate;
uniform float     u_trace_intensity;
uniform int       u_mode;

in  vec2 v_uv;

layout(location = 0) out vec4 out_display;
layout(location = 1) out vec4 out_persist;

const float MAX_TRACE = 0.6;
const float PIXEL_SIZE = 8.0;
const float DOT_SIZE = 28.0;
const float DOT_RADIUS = 0.42;
const float AIRPORT_SIZE = 20.0;
const float AIRPORT_RADIUS = 0.36;
const float EDGE_THRESHOLD = 0.15;

float edgeDetect(sampler2D tex, vec2 uv, vec2 texelSize) {
    float c = texture(tex, uv).r;
    float l = texture(tex, uv + vec2(-texelSize.x, 0.0)).r;
    float r = texture(tex, uv + vec2(texelSize.x, 0.0)).r;
    float t = texture(tex, uv + vec2(0.0, texelSize.y)).r;
    float b = texture(tex, uv + vec2(0.0, -texelSize.y)).r;
    return step(EDGE_THRESHOLD, abs(c-l) + abs(c-r) + abs(c-t) + abs(c-b));
}

vec3 rgb2hsv(vec3 c) {
    vec4 K = vec4(0.0, -1.0/3.0, 2.0/3.0, -1.0);
    vec4 p = mix(vec4(c.bg, K.wz), vec4(c.gb, K.xy), step(c.b, c.g));
    vec4 q = mix(vec4(p.xyw, c.r), vec4(c.r, p.yzx), step(p.x, c.r));
    float d = q.x - min(q.w, q.y);
    float e = 1.0e-10;
    return vec3(abs(q.z + (q.w - q.y) / (6.0 * d + e)), d / (q.x + e), q.x);
}

vec3 hsv2rgb(vec3 c) {
    vec4 K = vec4(1.0, 2.0/3.0, 1.0/3.0, 3.0);
    vec3 p = abs(fract(c.xxx + K.xyz) * 6.0 - K.www);
    return c.z * mix(K.xxx, clamp(p - K.xxx, 0.0, 1.0), c.y);
}

void main() {
    float persist = texture(u_persistence, v_uv).r;
    float sil = texture(u_silhouette, v_uv).r;
    float decayed = persist * u_fade_rate;
    float accumulated;
    if (u_mode == 8) {
        // Airport mode: snap on, slow fade off
        vec2 dims = vec2(textureSize(u_fossil, 0));
        vec2 cp = v_uv * dims / AIRPORT_SIZE;
        vec2 cid = floor(cp);
        vec2 jit = vec2(
            fract(sin(dot(cid, vec2(127.1, 311.7))) * 43758.5453) - 0.5,
            fract(sin(dot(cid, vec2(269.5, 183.3))) * 43758.5453) - 0.5
        ) * 0.3;
        vec2 cc = cid + 0.5 + jit;
        vec2 cellUV = cc * AIRPORT_SIZE / dims;
        float cellSil = texture(u_silhouette, cellUV).r;
        if (cellSil > 0.05) {
            // Snap on instantly
            accumulated = 1.0;
        } else {
            // Slow fade
            accumulated = max(persist - 0.015, 0.0);
        }
    } else if (u_mode == 10) {
        // Lens mode: use standard accumulation
        accumulated = min(decayed + sil * u_trace_intensity, MAX_TRACE);
    } else if (u_mode == 14) {
        // 3D: 6x faster fade
        float f = u_fade_rate * u_fade_rate * u_fade_rate;
        accumulated = min(persist * f * f + sil * u_trace_intensity, MAX_TRACE);
    } else {
        accumulated = min(decayed + sil * u_trace_intensity, MAX_TRACE);
    }

    vec4 fossil = texture(u_fossil, v_uv);
    vec3 result = fossil.rgb;

    if (u_mode == 0) {
        result = max(fossil.rgb - vec3(accumulated * 1.5), vec3(0.0));
    } else if (u_mode == 1) {
        result = mix(fossil.rgb, vec3(1.0), accumulated);
    } else if (u_mode == 2) {
        vec2 ts = 1.0 / vec2(textureSize(u_silhouette, 0));
        float edge = edgeDetect(u_silhouette, v_uv, ts * 3.0);
        float ea = min(decayed + edge * u_trace_intensity * 2.0, MAX_TRACE);
        accumulated = ea;
        result = max(fossil.rgb - vec3(ea * 2.0), vec3(0.0));
    } else if (u_mode == 3) {
        vec3 hsv = rgb2hsv(fossil.rgb);
        hsv.x = fract(hsv.x + accumulated * 0.4);
        hsv.y = min(hsv.y + accumulated * 0.3, 1.0);
        result = hsv2rgb(hsv);
    } else if (u_mode == 4) {
        float band = sin(v_uv.y * 3.14159 * 5.0) * 0.5 + 0.5;
        float sf = mix(u_fade_rate * 0.98, u_fade_rate, band);
        float sa = min(persist * sf + sil * u_trace_intensity, MAX_TRACE);
        accumulated = sa;
        vec3 tint = mix(vec3(0.0, 0.02, 0.12), vec3(0.15, 0.05, 0.0), v_uv.y);
        result = max(fossil.rgb - vec3(sa * 1.2) + tint * sa, vec3(0.0));
    } else if (u_mode == 5) {
        vec3 underneath = 1.0 - vec3(dot(fossil.rgb, vec3(0.299, 0.587, 0.114)));
        result = mix(fossil.rgb, underneath, accumulated);
    } else if (u_mode == 6) {
        accumulated = min(persist * u_fade_rate * u_fade_rate + sil * u_trace_intensity, MAX_TRACE);
        vec2 dims = vec2(textureSize(u_fossil, 0));
        // Sample depth at this pixel to determine pixel block size
        vec3 depthRGB = texture(u_depth_persist, v_uv).rgb;
        // Invert — close objects should get big pixels
        float depthVal = 1.0 - dot(depthRGB, vec3(0.299, 0.587, 0.114));
        // Closer = bigger pixels, farther = smaller
        // Range: 4px (far) to 40px (close)
        float pixSize = mix(4.0, 40.0, depthVal * smoothstep(0.0, 0.2, accumulated));
        pixSize = clamp(pixSize, 4.0, 40.0);
        // Snap to pixel grid at this size
        vec2 pUV = floor(v_uv * dims / pixSize) * pixSize / dims;
        vec4 px = texture(u_fossil, pUV);
        result = mix(fossil.rgb, px.rgb, smoothstep(0.0, 0.3, accumulated));
        result *= (1.0 - accumulated * 0.3);
    } else if (u_mode == 7) {
        vec2 dims = vec2(textureSize(u_fossil, 0));
        vec2 cp = v_uv * dims / DOT_SIZE;
        vec2 cid = floor(cp);
        vec2 jit = vec2(
            fract(sin(dot(cid, vec2(127.1, 311.7))) * 43758.5453) - 0.5,
            fract(sin(dot(cid, vec2(269.5, 183.3))) * 43758.5453) - 0.5
        ) * 0.3;
        vec2 cc = cid + 0.5 + jit;
        vec2 cuv = cc * DOT_SIZE / dims;
        float dd = length(cp - cc);
        float rv = DOT_RADIUS + (fract(sin(dot(cid, vec2(53.1, 97.3))) * 43758.5453) - 0.5) * 0.08;
        float ic = 1.0 - smoothstep(rv - 0.04, rv, dd);
        vec3 dc = texture(u_fossil, cuv).rgb;
        vec3 gr = fossil.rgb * 0.4;
        result = mix(fossil.rgb, mix(gr, dc, ic), smoothstep(0.0, 0.25, accumulated));
        result *= (1.0 - accumulated * 0.15);
    } else if (u_mode == 8) {
        // Airport: depth-colored dots, snap on, slow fade
        vec2 dims = vec2(textureSize(u_fossil, 0));
        vec2 cp = v_uv * dims / AIRPORT_SIZE;
        vec2 cid = floor(cp);
        vec2 jit = vec2(
            fract(sin(dot(cid, vec2(127.1, 311.7))) * 43758.5453) - 0.5,
            fract(sin(dot(cid, vec2(269.5, 183.3))) * 43758.5453) - 0.5
        ) * 0.3;
        vec2 cc = cid + 0.5 + jit;
        vec2 cellUV = cc * AIRPORT_SIZE / dims;
        float dd = length(cp - cc);
        float rv = AIRPORT_RADIUS + (fract(sin(dot(cid, vec2(53.1, 97.3))) * 43758.5453) - 0.5) * 0.08;
        float ic = 1.0 - smoothstep(rv - 0.04, rv, dd);

        vec3 depthColor = texture(u_depth_persist, cellUV).rgb;
        depthColor = min(depthColor * 2.0, vec3(1.0));
        if (length(depthColor) < 0.15) depthColor = vec3(0.85, 0.65, 0.3);

        if (accumulated < 0.01) {
            result = fossil.rgb;
        } else {
            result = mix(fossil.rgb, mix(fossil.rgb, depthColor, ic), accumulated);
        }
    } else if (u_mode == 10) {
        // Lens: rippled glass distortion, no circles
        // Use silhouette gradient to displace fossil UVs
        // Sample silhouette gradient over wider area for stronger displacement
        vec2 texel = 1.0 / vec2(textureSize(u_silhouette, 0));
        float spread = 20.0;
        float sl = texture(u_silhouette, v_uv + vec2(-texel.x * spread, 0.0)).r;
        float sr = texture(u_silhouette, v_uv + vec2( texel.x * spread, 0.0)).r;
        float st = texture(u_silhouette, v_uv + vec2(0.0,  texel.y * spread)).r;
        float sb = texture(u_silhouette, v_uv + vec2(0.0, -texel.y * spread)).r;
        vec2 grad = vec2(sr - sl, st - sb);
        // Strong displacement
        float strength = accumulated * 0.5;
        vec2 lensUV = v_uv + grad * strength;
        vec3 lensColor = texture(u_fossil, lensUV).rgb;
        result = lensColor;
    } else if (u_mode == 11) {
        // Lens 2: extreme version of Lens 1 (10x)
        vec2 texel = 1.0 / vec2(textureSize(u_silhouette, 0));
        float spread = 200.0;
        float sl = texture(u_silhouette, v_uv + vec2(-texel.x * spread, 0.0)).r;
        float sr = texture(u_silhouette, v_uv + vec2( texel.x * spread, 0.0)).r;
        float st = texture(u_silhouette, v_uv + vec2(0.0,  texel.y * spread)).r;
        float sb = texture(u_silhouette, v_uv + vec2(0.0, -texel.y * spread)).r;
        vec2 grad = vec2(sr - sl, st - sb);
        float strength = accumulated * 5.0;
        vec2 lensUV = v_uv + grad * strength;
        vec3 lensColor = texture(u_fossil, lensUV).rgb;
        result = lensColor;
    } else if (u_mode == 12) {
        // Lens 3: moderate distortion, no doubling
        vec2 texel = 1.0 / vec2(textureSize(u_silhouette, 0));
        float spread = 40.0;
        float sl = texture(u_silhouette, v_uv + vec2(-texel.x * spread, 0.0)).r;
        float sr = texture(u_silhouette, v_uv + vec2( texel.x * spread, 0.0)).r;
        float st = texture(u_silhouette, v_uv + vec2(0.0,  texel.y * spread)).r;
        float sb = texture(u_silhouette, v_uv + vec2(0.0, -texel.y * spread)).r;
        vec2 grad = vec2(sr - sl, st - sb);
        float strength = accumulated * 1.5;
        vec2 lensUV = v_uv + grad * strength;
        vec3 lensColor = texture(u_fossil, lensUV).rgb;
        result = lensColor;
    } else if (u_mode == 13) {
        // Topo 1: bold depth-colored contour lines
        // Heavy blur for smooth interior gradient
        vec2 texel = 1.0 / vec2(textureSize(u_silhouette, 0));
        float elevation = 0.0;
        float total = 0.0;
        for (int y = -4; y <= 4; y++) {
            for (int x = -4; x <= 4; x++) {
                float w = 1.0 / (1.0 + float(x*x + y*y));
                elevation += texture(u_silhouette, v_uv + vec2(float(x), float(y)) * texel * 12.0).r * w;
                total += w;
            }
        }
        elevation /= total;
        // Bold contour lines — fewer, thicker
        float numLines = 5.0;
        float contour = fract(elevation * numLines);
        float line = 1.0 - smoothstep(0.15, 0.35, contour) * (1.0 - smoothstep(0.65, 0.85, contour));
        // Color lines with depth colormap
        vec3 depthColor = texture(u_depth_persist, v_uv).rgb;
        depthColor = min(depthColor * 2.5, vec3(1.0));
        if (length(depthColor) < 0.1) depthColor = vec3(0.85, 0.5, 0.2);
        // Bold lines in depth color over darkened fossil
        vec3 topoResult = mix(fossil.rgb * 0.6, depthColor, line);
        // Use accumulated for persistence, sil for fresh contour data
        float visibility = max(smoothstep(0.0, 0.1, sil), smoothstep(0.0, 0.15, accumulated));
        result = mix(fossil.rgb, topoResult, visibility * 0.5);
    } else if (u_mode == 14) {
        // 3D: Glass-man — depth-based refraction across entire body
        vec2 texel = 1.0 / vec2(textureSize(u_depth_persist, 0));

        // Compute smooth normals from DEPTH colormap (wide spread = less contours)
        float spread = 15.0;
        float dl = dot(texture(u_depth_persist, v_uv + vec2(-texel.x * spread, 0.0)).rgb, vec3(0.299, 0.587, 0.114));
        float dr = dot(texture(u_depth_persist, v_uv + vec2( texel.x * spread, 0.0)).rgb, vec3(0.299, 0.587, 0.114));
        float dt = dot(texture(u_depth_persist, v_uv + vec2(0.0,  texel.y * spread)).rgb, vec3(0.299, 0.587, 0.114));
        float db = dot(texture(u_depth_persist, v_uv + vec2(0.0, -texel.y * spread)).rgb, vec3(0.299, 0.587, 0.114));
        vec2 depthNormal = vec2(dr - dl, dt - db);
        // Limit extreme normals at depth boundaries
        float normalLen = length(depthNormal);
        if (normalLen > 0.2) depthNormal *= 0.2 / normalLen;

        // Refraction strength tracks depth — closer parts refract more
        float depthHere = dot(texture(u_depth_persist, v_uv).rgb, vec3(0.299, 0.587, 0.114));
        float strength = accumulated * (0.3 + depthHere * 1.2);

        // Chromatic aberration through depth-derived surface
        vec2 dispR = clamp(v_uv + depthNormal * strength * 1.15, 0.0, 1.0);
        vec2 dispG = clamp(v_uv + depthNormal * strength, 0.0, 1.0);
        vec2 dispB = clamp(v_uv + depthNormal * strength * 0.85, 0.0, 1.0);
        result.r = texture(u_fossil, dispR).r;
        result.g = texture(u_fossil, dispG).g;
        result.b = texture(u_fossil, dispB).b;

        // Specular highlights from depth surface (whole body, not just edges)
        vec2 lightDir = normalize(vec2(0.6, 0.8));
        float spec = pow(max(dot(normalize(depthNormal + 0.001), lightDir), 0.0), 32.0);
        result += vec3(1.0, 0.95, 0.9) * spec * 0.3 * smoothstep(0.0, 0.1, accumulated);
    } else if (u_mode == 9) {
        // Depth smudge — blends toward persistent depth color
        vec3 depthColor = texture(u_depth_persist, v_uv).rgb;
        // Smooth blend ramp to avoid harsh silhouette edge
        float blend = smoothstep(0.0, 0.25, accumulated);
        result = mix(fossil.rgb, depthColor, blend);
    }

    out_display = vec4(result, 1.0);
    out_persist = vec4(accumulated, 0.0, 0.0, 1.0);
}
