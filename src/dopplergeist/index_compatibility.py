"""Source-pinned equivalent prepared-index normalization implementations."""

# Listed source revisions have been checked against recorded observations;
# unlisted edits disable the prepared-track fast path.
PREPARED_DATA_ACCESS_EQUIVALENCE = {
    # Archive intake now excludes partial .readsb_tmp members; prepared-track
    # decoding and normalization are unchanged from the prior source.
    "92ea0983dc869a815fc872ad1be56441421f7764db98d2b2a676ab5fe0c469d5": (
        "690caf2155bcc17905a5d71ea3b3d4111485451cc734b827c9c4ac5f2997d22b",
    ),
    # Acquisition progress callbacks report counters and do not change recorded
    # observation decoding, normalization, or prepared track construction.
    "6f1927a9daa638748207abb0479031baa01f6aee1fcedcfbf74280111cb0b47d": (
        "690caf2155bcc17905a5d71ea3b3d4111485451cc734b827c9c4ac5f2997d22b",
        "23aa4ac1781cb5be921f5243f421886d2f2e549a4ded6dcc1986ade651883f7e",
        "d88c4be6668b92c9a1a31b5cd0b4dbdcb3d7cf73c5891ae414f44deca6088644",
        "77da0d39e1e95476fdf02e29e9a345914545af7e62227a93b4cb6786edb37a71",
        "ddd77e9734fff6a70cc9195bd1305002f9f78e39ea601f27e0f596dcb336033a",
        "275e38a37f82993b609b6b78c113d35c4da269083ceb3afcb1d94ada48bdc0d4",
        "dad02d09d89ad9d267cd8d0424b8e018d0399a28d6b43903b5a55867290b3957",
        "d664caf4b496599d29a81360c4a9d9736a3248c157e71d885463b349aad6dd36",
        "ce78ad66f2d478e1f7ea158ea04c0d3b25021339e34f090652740fe55e46e5ec",
    ),
    # Only GLO-90 terrain tile selection changed; prepared aircraft
    # observation decoding and normalization remain byte-for-byte unchanged.
    "690caf2155bcc17905a5d71ea3b3d4111485451cc734b827c9c4ac5f2997d22b": (
        "23aa4ac1781cb5be921f5243f421886d2f2e549a4ded6dcc1986ade651883f7e",
        "d88c4be6668b92c9a1a31b5cd0b4dbdcb3d7cf73c5891ae414f44deca6088644",
        "77da0d39e1e95476fdf02e29e9a345914545af7e62227a93b4cb6786edb37a71",
        "ddd77e9734fff6a70cc9195bd1305002f9f78e39ea601f27e0f596dcb336033a",
        "275e38a37f82993b609b6b78c113d35c4da269083ceb3afcb1d94ada48bdc0d4",
        "dad02d09d89ad9d267cd8d0424b8e018d0399a28d6b43903b5a55867290b3957",
        "d664caf4b496599d29a81360c4a9d9736a3248c157e71d885463b349aad6dd36",
        "ce78ad66f2d478e1f7ea158ea04c0d3b25021339e34f090652740fe55e46e5ec",
    ),
    # Country selection changes only acquisition bounds and optional row warming;
    # prepared observation decoding and normalization are unchanged.
    "23aa4ac1781cb5be921f5243f421886d2f2e549a4ded6dcc1986ade651883f7e": (
        "d88c4be6668b92c9a1a31b5cd0b4dbdcb3d7cf73c5891ae414f44deca6088644",
        "77da0d39e1e95476fdf02e29e9a345914545af7e62227a93b4cb6786edb37a71",
        "ddd77e9734fff6a70cc9195bd1305002f9f78e39ea601f27e0f596dcb336033a",
        "275e38a37f82993b609b6b78c113d35c4da269083ceb3afcb1d94ada48bdc0d4",
        "dad02d09d89ad9d267cd8d0424b8e018d0399a28d6b43903b5a55867290b3957",
        "d664caf4b496599d29a81360c4a9d9736a3248c157e71d885463b349aad6dd36",
        "ce78ad66f2d478e1f7ea158ea04c0d3b25021339e34f090652740fe55e46e5ec",
    ),
    # Component hashing and exact geometry-source lookup preserve normalization.
    "d88c4be6668b92c9a1a31b5cd0b4dbdcb3d7cf73c5891ae414f44deca6088644": (
        "77da0d39e1e95476fdf02e29e9a345914545af7e62227a93b4cb6786edb37a71",
        "ddd77e9734fff6a70cc9195bd1305002f9f78e39ea601f27e0f596dcb336033a",
        "275e38a37f82993b609b6b78c113d35c4da269083ceb3afcb1d94ada48bdc0d4",
        "dad02d09d89ad9d267cd8d0424b8e018d0399a28d6b43903b5a55867290b3957",
        "d664caf4b496599d29a81360c4a9d9736a3248c157e71d885463b349aad6dd36",
        # The earlier revision differs only in release-asset discovery.
        "ce78ad66f2d478e1f7ea158ea04c0d3b25021339e34f090652740fe55e46e5ec",
    ),
    # Read-only digest fallback changes no observation decoding or normalization.
    "77da0d39e1e95476fdf02e29e9a345914545af7e62227a93b4cb6786edb37a71": (
        "ddd77e9734fff6a70cc9195bd1305002f9f78e39ea601f27e0f596dcb336033a",
        "275e38a37f82993b609b6b78c113d35c4da269083ceb3afcb1d94ada48bdc0d4",
        "dad02d09d89ad9d267cd8d0424b8e018d0399a28d6b43903b5a55867290b3957",
        "d664caf4b496599d29a81360c4a9d9736a3248c157e71d885463b349aad6dd36",
    ),
    # The native collector only replaces canonical Python column extraction.
    "ddd77e9734fff6a70cc9195bd1305002f9f78e39ea601f27e0f596dcb336033a": (
        "275e38a37f82993b609b6b78c113d35c4da269083ceb3afcb1d94ada48bdc0d4",
        "dad02d09d89ad9d267cd8d0424b8e018d0399a28d6b43903b5a55867290b3957",
        "d664caf4b496599d29a81360c4a9d9736a3248c157e71d885463b349aad6dd36",
    ),
    "275e38a37f82993b609b6b78c113d35c4da269083ceb3afcb1d94ada48bdc0d4": (
        "dad02d09d89ad9d267cd8d0424b8e018d0399a28d6b43903b5a55867290b3957",
        "d664caf4b496599d29a81360c4a9d9736a3248c157e71d885463b349aad6dd36",
    ),
}

# #329 adds a CPA-only support method without changing Track initialization,
# decoded observations, velocities, or prepared payload construction.
PREPARED_GEOMETRY_EQUIVALENCE = {
    # Time-scoped CPA exclusion changes the proof's query scope, not the
    # prepared observation payload, interpolation, or track geometry.
    "36b0cb62e11c2d50d40b5b5ac5c54ada06b7120aa9f11766bc6346c388816bf7": (
        "aff5eb70f00766a1a3ab890d1ba33f1a4989af9cf1c4b628770a8d2a7767fd13",
        "85336bd0e3739f890386e9f93965cefb78c4477ae2e8a4fe489353501c5e26a2",
    ),
    # Finite-distance guards change only the exclusion proof, not prepared tracks.
    "76bb80b7e3c9d19246a2dd6095795955a1420c54d1b0cea99b635c6005dc728c": (
        "aff5eb70f00766a1a3ab890d1ba33f1a4989af9cf1c4b628770a8d2a7767fd13",
        "85336bd0e3739f890386e9f93965cefb78c4477ae2e8a4fe489353501c5e26a2",
    ),
    "aff5eb70f00766a1a3ab890d1ba33f1a4989af9cf1c4b628770a8d2a7767fd13": (
        "85336bd0e3739f890386e9f93965cefb78c4477ae2e8a4fe489353501c5e26a2",
    ),
}
