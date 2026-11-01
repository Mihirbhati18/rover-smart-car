# Depth Anything V2 indoor-small: stationary bottle check

Date: 2026-10-09 (Asia/Calcutta). Phone image rotated 180 degrees in software.
Model: official Depth Anything V2 Metric Hypersim Small, input size 392, CPU.
These are exploratory observations, not safe driving distances.

| Bottle distance reported by user, from car front | Model at bottle body | Model near bottle base | Image |
| --- | ---: | ---: | --- |
| About 80–90 cm | 1.55 m | 1.53 m | `../depth_input_bottle_clear.jpg` |
| Target about 40–50 cm (not measured; user confirmed placement) | 1.10 m | 1.03 m | `../depth_input_bottle_close.jpg` |
| Target about 1.2 m (not measured; user confirmed placement) | 2.21 m | 2.21 m | `../depth_input_bottle_far.jpg` |

The model estimates increase in the expected order as the bottle moves farther
away. The near-to-middle estimate changed by 45 cm, but the middle-to-far
estimate changed by 66 cm. Absolute estimates are substantially higher than
the reported or requested distances from the car front, and the discrepancy
is not a single consistent offset. The phone lens is behind the car front,
but that offset was not measured. Three approximate placements of one object
in one room do not establish accuracy or a safe threshold. People were also
partly visible in the frames and may affect predictions.

The depth preview has no connection to Arduino serial or motor control.
Keep ultrasonic as an independent stop condition. Do not use these results to
authorize movement until tests cover multiple distances, objects, lighting,
and the actual floor setup.

## Floor-view check, 2026-10-10

With the car stationary on the floor and its motor battery off, the user placed
the bottle approximately 60–80 cm ahead in the centre of the camera view.
The unmarked image is
`../active_view_frames/bottle-centered-2026-10-10/raw-view-1.jpg`.
The model estimated about 0.81 m on the bottle body and 0.79 m near its base;
these are not independently measured distances. It estimated 2.20 m at a
left-side floor pixel and 1.10 m at a right-side floor/clutter pixel. The
three-picture color scorer tentatively suggested LEFT, consistent with the
visible clutter on the right. Neither method verifies a traversable route or
a turning angle. No motor commands were sent during the camera check.
