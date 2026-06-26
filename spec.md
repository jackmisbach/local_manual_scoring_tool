Can we make a repo in METR org called local_manual_scoring_tool that is a stripped down version of the METR/manual-scoring-tool repo that assumes the project dirs are on disk, does not use hawk or hawk api, and saves scores to a local jsonl file. When you make the repo make it with permission for admins to push to main

The things it does do: 
- Opens up the VSCode container and puts the project dir in there
- Has the frontend

Cut out anything you don't need from the manual-scoring-tool repo. 