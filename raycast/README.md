# Findstill for Raycast

Search your Apple Photos library by describing a picture, then open the one you want in Photos.

The extension needs [Findstill](https://github.com/ABCastor/findstill) running on the same Mac. Findstill indexes your library locally with Google's SigLIP2 image model; this extension only talks to that local service at `http://127.0.0.1:18473`, which you can change in the extension's preferences. Nothing is sent anywhere else.

Enter opens the selected photo in Apple Photos. Other actions show a larger preview, copy the photo ID or open the Findstill dashboard. The dropdown limits results to photos or videos; an empty search shows your most recent pictures.
