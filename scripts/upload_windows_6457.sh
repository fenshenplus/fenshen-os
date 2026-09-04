#!/bin/zsh
set -e
ROOT=/Users/a13401098230/WorkBuddy/fenshen-v1
SITE=$ROOT/site
ZIP=$SITE/分身-v0.64.57-Windows-exe.zip

cd $SITE
rm -f default.exe 分身-v0.64.57-Windows.exe $ZIP
gh release download v0.64.57 --repo fenshenplus/fenshen-os --pattern "default.exe"
mv default.exe 分身-v0.64.57-Windows.exe
zip -j $ZIP 分身-v0.64.57-Windows.exe
rm 分身-v0.64.57-Windows.exe
sshpass -p 'Abba7481?' scp -o StrictHostKeyChecking=no $ZIP root@47.111.25.150:/opt/fenshen-relay/

# Update landing page Windows link back to v0.64.57
sed -i '' 's/fenshen-v0\.64\.56-Windows-exe\.zip/fenshen-v0.64.57-Windows-exe.zip/g' $SITE/index.html
sed -i '' 's/Windows 版（v0\.64\.56/Windows 版（v0.64.57/g' $SITE/index.html
sshpass -p 'Abba7481?' scp -o StrictHostKeyChecking=no $SITE/index.html root@47.111.25.150:/opt/fenshen-relay/index.html

echo "Windows v0.64.57 uploaded and landing page updated"
