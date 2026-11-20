"""Deliver learner profiles and recommendations to the Moodle plugins.

    python -m moodle_sync bundle   build the import bundle only
    python -m moodle_sync push     build it, copy it into the Moodle web
                                   container and run the plugin's import CLI

No network service is involved: the plugin ``local_microlearning`` imports the
bundle into its own tables, and the block and the teacher pages read those.
"""
