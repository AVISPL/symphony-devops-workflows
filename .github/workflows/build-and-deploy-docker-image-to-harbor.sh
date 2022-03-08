#!/bin/bash
# usage:
# ./build-and-deploy-docker-image-to-harbor[-m <microservice>]

for ARG in "$@"
do
	if [ "${ARG}" = "-m" ]; then
		# next element is branch name
		ARG_MICROSERVICE=true
	elif [ "$ARG_MICROSERVICE" = true ]; then
		MICROSERVICE="${ARG}"
		ARG_MICROSERVICE=false
	fi
done

if [ -z "$MICROSERVICE" ]; then 
	echo "microservice argument cannot be null...Unable to build and tag docker image!"; 
else 
	echo "Building and tagging Docker image for ${MICROSERVICE}";
	
	# determine version
	PROJECT_VERSION=$(mvn -B -q -f . -Dexec.executable="echo" -Dexec.args='${project.version}' --non-recursive exec:exec)
	# remove "SNAPSHOT" from PROJECT_VERSION and append current yymmddHHMM to it
	VERSION="${PROJECT_VERSION%SNAPSHOT}$(echo $(date +%y%m%d%H%M) | cut -c 1-11)"
	
	echo "building ${MICROSERVICE} v.${VERSION} image"
	#sudo docker build -t ${MICROSERVICE}:${VERSION} .
	
	echo "tagging ${MICROSERVICE} v.${VERSION} image"
	#sudo docker tag ${MICROSERVICE}:${VERSION} registry.vnocsymphony.com/symphony-microservices/${MICROSERVICE}:${VERSION}
	
	echo "pushing ${MICROSERVICE} v.${VERSION} tag to registry"
	#sudo docker push registry.vnocsymphony.com/symphony-microservices/${MICROSERVICE}:${VERSION}
	
	echo "tagging ${MICROSERVICE} SNAPSHOT image"
	#sudo docker tag registry.vnocsymphony.com/symphony-microservices/${MICROSERVICE}:${VERSION} registry.vnocsymphony.com/symphony-microservices/${MICROSERVICE}:snapshot
	
	echo "pushing ${MICROSERVICE} SNAPSHOT tag to registry"
	#sudo docker push registry.vnocsymphony.com/symphony-microservices/${MICROSERVICE}:snapshot
	
	# Cleanup old images, if any. Following will locate and remove old ${MICROSERVICE} images, excluding one marked as snapshot, and ones marked with version this script is running under
	#sudo docker rmi -f $(sudo docker images | grep -E "${MICROSERVICE}" | grep -v ${VERSION} | grep -v snapshot | awk '{printf"%s ",$3}')
fi
