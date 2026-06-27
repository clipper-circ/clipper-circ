#!/bin/bash
cd "$(dirname "$0")"
echo "What changed? (brief description):"
read msg
git add -A
git commit -m "$msg"
git push
echo ""
echo "Done! Railway is deploying your changes. Check in ~60 seconds."
