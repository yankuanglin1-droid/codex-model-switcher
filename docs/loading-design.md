# Startup surface

Rules: use a native macOS adaptive blur material, a light frosted panel matching
the main UI, the existing icon and a short text hierarchy. Keep depth subtle.
No fake completion percentage or minimum display time. Respect system material
accessibility and reduce animation on the Windows surface.

Avoid: heavy shadows, unrelated gradients over the native material, small gray
error text, decorative progress that completes before settings load.

Implementation: macOS NSVisualEffectView with a centered 480pt panel, 84pt icon,
26pt title, 13pt live status and a system spinner. Dismiss after the web UI reports
that state loaded, rather than merely after HTML navigation. Failure or timeout
shows a retry control. Windows uses a CSS frosted loading surface inside WebView2;
it does not promise desktop wallpaper blur identical to macOS.

Checklist: inspect real app preview; verify title/icon spacing, readable status,
resize behavior, failure/retry handling, no private data in preview, no artificial
delay, and successful transition to the model screen.

Design prompt skeleton: white-led monochrome desktop startup surface, translucent
frosted system material, centered existing product mark and title, abundant empty
space, small truthful loading status, minimal border, no heavy shadow or fake
percentage. This is an implementation brief, not an image-generation request.
