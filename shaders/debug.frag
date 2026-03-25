#version 330 core

uniform sampler2D u_texture;
uniform float u_alpha;

in  vec2 v_uv;
out vec4 out_color;

void main() {
    vec3 col = texture(u_texture, v_uv).rgb;
    // Only show where there's depth data (non-black pixels)
    float has_data = step(0.01, dot(col, vec3(1.0)));
    out_color = vec4(col, u_alpha * has_data);
}
